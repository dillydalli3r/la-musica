"""The whole check / grading / optimization stack, described once, edited here.

MAINTAIN -> Check stack (``web/src/pages/CheckStackPage.tsx``) renders this
payload and saves through it: which library scripts run and in what order,
which grading checks are on, what each named preset turns on, and what the
audit pass steps are.

Nothing in this module is a second copy of a list. Every fact is read out of
the module that owns it:

  scripts   ``server.script_runners.RUNNERS`` — the ids /api/run accepts and
            the labels a run prints — plus ``mlo.cli.SCRIPTS`` (the names and
            one-line descriptions every menu shows), ``mlo.cli.SCRIPT_GATES``
            (the feature switch that makes a chain skip a script) and the
            ``run_all_order`` key that decides membership and order.
  checks    ``server.tags_registry.registry()``, which itself is built from
            ``mlo.config.DEFAULT_CONFIG`` and the grader's own tables, with the
            gate set ``mlo.grader.check_gates()`` reads out of its source. The
            two sets must agree; tools/test_check_stack.py is what says so.
  audit     ``mlo.audit``'s tables (``DETECTOR_NO_FLAGS``, and the ``audit_*``
            names its own source mentions) plus every ``audit_*`` key
            DEFAULT_CONFIG holds.

GET /api/stack  the description above, each entry carrying its CURRENT value,
                its default, the group it is shown in and the presets it
                belongs to.
PUT /api/stack  edits, written to the config keys the app already uses
                (``run_all_order``, the ``grade_check_*`` / ``audit_*``
                booleans, the per-script feature switches). An unknown id is
                REFUSED with 400 and nothing is written: a client that has
                drifted from the code must be told, not quietly obeyed.
"""
from __future__ import annotations

import ast
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# _humanize is the one key->label humanizer in the tree (server/tags_registry),
# so a check or an audit key with no table entry is named the same way here.
from server.tags_registry import _humanize, registry

from mlo.cli import SCRIPTS, SCRIPT_GATES
from mlo.config import (DEFAULT_CONFIG, DEFAULT_RUN_ALL_ORDER, load_config,
                        normalize_config, save_config)
from mlo.grader import check_gates
from server.script_runners import RUNNERS

router = APIRouter()

# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #
# id -> (menu name, what the script does). The MENU name is what
# web/src/lib/scripts.ts and README.md mirror, so it is the label the stack
# page shows; RUNNERS still supplies the id set and the run-time label.
_MENU = {sid: (name, desc) for sid, name, desc in SCRIPTS}


def _gate_keys(sid: int) -> List[str]:
    """The config switches that make a chain skip *sid* (any-of: a tuple means
    the script runs while ANY of them is on — 17 does transliteration,
    translation, or both)."""
    gate = SCRIPT_GATES.get(sid)
    if gate is None:
        return []
    return [gate] if isinstance(gate, str) else list(gate)


_anchored_cache: Optional[set] = None


def _anchored_ids() -> set:
    """The script ids mlo.config puts back into ``run_all_order`` on every
    load: normalize_config keeps a saved 1..11 sequence and re-anchors the
    later ids at their canonical positions (``_insert_script``), so dropping
    one of them from the order does not stick.

    Asked, not copied: normalize a one-entry order and see which ids come
    back. If the anchors change there, this answer changes with them."""
    global _anchored_cache
    if _anchored_cache is None:
        kept = [min(RUNNERS)]
        _anchored_cache = set(
            normalize_config({"run_all_order": kept})["run_all_order"]) - set(kept)
    return _anchored_cache


def _script_entry(sid: int, cfg: dict, order: List[int]) -> dict:
    label, runner = RUNNERS[sid]
    name, desc = _MENU.get(sid, (label, ""))
    gates = _gate_keys(sid)
    gated_on = all(bool(cfg.get(k, True)) for k in gates) if gates else True
    return {
        "id": sid,
        "label": name,
        "description": desc,
        "in_order": sid in order,
        # A script runs in Run All only when it holds a slot in the order AND
        # its feature switch is on — with the switch off the chain skips it
        # (server/script_runners._DISABLED), which is why a ticked script can
        # still do nothing.
        "enabled": sid in order and gated_on,
        "order": order.index(sid) if sid in order else None,
        "gate": {"keys": gates, "enabled": gated_on},
        # False for the ids the app re-anchors: only a feature switch can keep
        # one of those out of a run.
        "removable": sid not in _anchored_ids(),
        "available": bool(runner),
    }


def _scripts(cfg: dict, order: List[int]) -> List[dict]:
    return [_script_entry(sid, cfg, order) for sid in sorted(RUNNERS)]


def _with_script(order: List[int], sid: int, on: bool) -> List[int]:
    """Tick / untick a script in the Run All order. A re-ticked script goes
    back to its default pipeline position instead of jumping to the end (the
    same rule Settings -> Scripts uses)."""
    if not on:
        return [i for i in order if i != sid]
    at = len([i for i in order
              if DEFAULT_RUN_ALL_ORDER.index(i) < DEFAULT_RUN_ALL_ORDER.index(sid)])
    return order[:at] + [sid] + order[at:]


def _move(order: List[int], sid: int, index: int) -> List[int]:
    rest = [i for i in order if i != sid]
    return rest[:index] + [sid] + rest[index:]


# --------------------------------------------------------------------------- #
# Checks — the group each one is shown in
# --------------------------------------------------------------------------- #
# The groups mirror Settings -> Grading (web/src/pages/GradingPage.tsx), the
# page that explains one check in prose: a check can only change group in both
# places at once, because tools/test_check_stack.py compares this map against
# that file's own groups. A check these rules do not claim lands in "tracks",
# which is where a per-track/album check belongs; a check in NO group (the
# Grading page's "Other checks" section) is a failure the test names.
GROUPS = (
    ("tracks", "Tracks & albums"),
    ("artist", "Artist"),
    ("auditing", "Auditing"),
    ("links", "Identity links"),
    ("covers", "Covers"),
    ("formatting", "Strict formatting"),
    ("lyrics", "Lyrics & translations"),
    ("categories", "File categories"),
)

_GROUP_KEYS = {
    "artist": ("grade_check_artist_image", "grade_check_artist_description"),
    "auditing": ("grade_check_audit", "grade_check_log_checksum",
                 "grade_check_accuraterip", "grade_check_log_grade"),
    "links": ("grade_check_mb_links", "grade_check_rym_links"),
    "covers": ("grade_check_cover", "grade_check_cover_crop",
               "grade_check_sidecar_cover"),
    # The formatting family is named by what it compares, not by what it
    # compares it ON, so the lyrics/cue spellings sit here while the lyrics
    # presence checks below the prefix rules: order matters, exact keys first.
    "formatting": ("grade_check_tag_spaces", "grade_check_tag_blank_lines",
                   "grade_check_lyrics_spaces", "grade_check_lyrics_blank_lines",
                   "grade_check_lyrics_zero", "grade_check_lyrics_format",
                   "grade_check_cue_spaces", "grade_check_cue_blank_lines",
                   "grade_check_cue_format", "grade_check_accurip_format",
                   "grade_check_cue_files"),
}
_GROUP_PREFIXES = (
    ("categories", "grade_include_"),
    ("lyrics", "grade_check_lyrics"),
    ("lyrics", "grade_check_xlit_"),
)


def group_of(key: str) -> str:
    """The group a check is shown under, from its own key."""
    for gid, keys in _GROUP_KEYS.items():
        if key in keys:
            return gid
    for gid, prefix in _GROUP_PREFIXES:
        if key.startswith(prefix):
            return gid
    return "tracks"


# --------------------------------------------------------------------------- #
# Presets — named starting points, still fully editable row by row
# --------------------------------------------------------------------------- #
PRESETS = (
    ("strict", "Strict", "Every check on (the file-category permissions keep "
                         "their configured value — what counts toward a grade "
                         "is the user's call)"),
    ("balanced", "Balanced", "The factory defaults, check by check"),
    ("relaxed", "Relaxed", "Only the essential checks: the strict formatting "
                           "family, the identity links and the content checks "
                           "nothing breaks without"),
)

# The checks "relaxed" switches off. Pinned against GradingPage.tsx's own
# `relaxedOff` list by tools/test_check_stack.py, so the two presets cannot
# drift apart.
RELAXED_OFF = frozenset((
    "grade_check_tag_spaces", "grade_check_lyrics_spaces",
    "grade_check_cue_spaces", "grade_check_cover_crop",
    "grade_check_lyrics_zero", "grade_check_tag_blank_lines",
    "grade_check_lyrics_blank_lines", "grade_check_cue_blank_lines",
    "grade_check_filename_case", "grade_check_ext_case",
    "grade_check_excess_tags", "grade_check_mb_links", "grade_check_rym_links",
    "grade_check_replaygain", "grade_check_album_description",
    "grade_check_artist_image", "grade_check_artist_description",
))


def preset_value(pid: str, key: str, default: bool) -> bool:
    """The value *pid* gives *key*. "strict" turns every real check on and
    leaves the file-category permissions at their configured value (a preset
    may force checks on; choosing what counts toward a grade is the user's
    call — the same rule Settings -> Grading applies)."""
    if pid == "strict":
        return True if not key.startswith("grade_include_") else default
    if pid == "balanced":
        return default
    return default and key not in RELAXED_OFF


def _gate_codes() -> dict:
    """The issue codes a check raises ITSELF, keyed by gate — from the grader's
    own tables (``TAG_PRESENCE_CHECKS`` names the code beside the gate, and the
    artist checks carry theirs). A check that grades a tag through a shared
    sweep has no code of its own to claim, so it claims none: the tag's code
    list would credit it with another check's verdicts."""
    from mlo.grader import TAG_PRESENCE_CHECKS, _ARTIST_CHECK_ISSUES
    out: Dict[str, set] = {}
    for _tag, (gate, code) in TAG_PRESENCE_CHECKS.items():
        out.setdefault(gate, set()).add(code)
    for gate, (code, _key) in _ARTIST_CHECK_ISSUES.items():
        out.setdefault(gate, set()).add(code)
    return {gate: sorted(codes) for gate, codes in out.items()}


def _describe(check: dict, codes: List[str]) -> str:
    """One line per check, built from what the registry and the grader already
    state: what it grades, the issue codes it raises on its own, and the key
    that turns it on. No second prose copy to drift from the grader."""
    bits = []
    if check["key"].startswith("grade_include_"):
        bits.append(f"decides whether {check['label'].lower()} count toward "
                    f"the album grade")
    elif check["tags"]:
        bits.append("grades " + ", ".join(check["tags"]))
    else:
        bits.append("graded on the album folder and its files rather than on "
                    "a tag")
    if codes:
        bits.append("issue codes " + ", ".join(codes))
    bits.append(f"toggle {check['key']} — "
                f"{'on' if check['default'] else 'off'} by default")
    return " · ".join(bits)


def _checks(cfg: dict) -> List[dict]:
    reg = registry()
    tags = {t["key"]: t for t in reg["tags"]}
    gate_codes = _gate_codes()
    out = []
    for check in reg["checks"]:
        graded = check["tags"]
        codes = gate_codes.get(check["key"], [])
        # The registry's writer string carries the stage too ("Auto tagging (8)
        # · genre import"); per CHECK the script identity is what matters, or
        # one script's stages would list four times on the page.
        writers = sorted({tags[t]["writer"].split(" · ")[0]
                          for t in graded if t in tags})
        row = dict(check)
        row.update({
            "group": group_of(check["key"]),
            "description": _describe(check, codes),
            "enabled": bool(cfg.get(check["key"], check["default"])),
            "issue_codes": codes,
            "writers": writers,
            "presets": [pid for pid, _, _ in PRESETS
                        if preset_value(pid, check["key"], check["default"])],
        })
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# The audit pass (script 6) — its own steps, from mlo.audit's tables
# --------------------------------------------------------------------------- #
_audit_source_cache: Optional[set] = None


def _audit_source_keys() -> set:
    """Every ``audit_*`` name mlo.audit's own source mentions — its config
    lookups and its own tables. A key DEFAULT_CONFIG holds that never appears
    here is a setting no audit step reads; the stack test reports it instead of
    the page offering a switch that does nothing."""
    global _audit_source_cache
    if _audit_source_cache is None:
        import mlo.audit as audit_mod
        with open(audit_mod.__file__, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=audit_mod.__file__)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("audit_"):
                    found.add(node.value)
        _audit_source_cache = found
    return _audit_source_cache


def _audit_keys() -> List[str]:
    return [k for k in DEFAULT_CONFIG if str(k).startswith("audit_")]


def _audit(cfg: dict) -> dict:
    from mlo.audit import DETECTOR_NO_FLAGS
    used = _audit_source_keys()
    steps = []
    for key in _audit_keys():
        default = DEFAULT_CONFIG[key]
        flag = DETECTOR_NO_FLAGS.get(key)
        kind = "bool" if isinstance(default, bool) else "number"
        if kind == "bool":
            bits = [f"toggle {key} — {'on' if default else 'off'} by default"]
        else:
            bits = [f"value {key} — {default} by default"]
        if flag:
            bits.append(f"switching it off appends {flag} to the scan")
        if key not in used:
            bits.append("NOT READ by mlo.audit — obsolete or renamed")
        steps.append({
            "key": key,
            "label": _humanize(key),
            "kind": kind,
            "default": default,
            "enabled": cfg.get(key, default),
            "flag": flag,
            "used": key in used,
            "description": " · ".join(bits),
        })
    sid = 6
    label, _runner = RUNNERS.get(sid, ("Audit library", None))
    name, desc = _MENU.get(sid, (label, ""))
    return {
        "script": {"id": sid, "label": name, "description": desc},
        "steps": steps,
    }


# --------------------------------------------------------------------------- #
# The payload
# --------------------------------------------------------------------------- #
def build_stack(cfg: Optional[dict] = None) -> dict:
    """The whole stack as the app currently has it."""
    cfg = load_config() if cfg is None else cfg
    order = [i for i in (cfg.get("run_all_order") or DEFAULT_RUN_ALL_ORDER)]
    scripts = _scripts(cfg, order)
    checks = _checks(cfg)
    per_group = {gid: [] for gid, _ in GROUPS}
    for check in checks:
        per_group.setdefault(check["group"], []).append(check["key"])
    return {
        "scripts": scripts,
        "run_all_order": order,
        "checks": checks,
        "groups": [{"id": gid, "title": title, "keys": per_group.get(gid, [])}
                   for gid, title in GROUPS],
        "presets": [{"id": pid, "label": label, "description": desc,
                     "keys": [c["key"] for c in checks
                              if preset_value(pid, c["key"], c["default"])]}
                    for pid, label, desc in PRESETS],
        "audit": _audit(cfg),
        "totals": {
            "scripts": len(scripts),
            "scripts_enabled": len([s for s in scripts if s["enabled"]]),
            "checks": len(checks),
            "checks_enabled": len([c for c in checks if c["enabled"]]),
        },
        # The grader's own gate set, so a client can see the two agree without
        # importing the grader: anything here that DEFAULT_CONFIG does not
        # define is a check the code reads and nothing can set.
        "grader_gates": check_gates(),
    }


@router.get("/api/stack")
def get_stack():
    """Every script and every grading check, with its default, its group, the
    presets it belongs to, and what the app has it set to right now."""
    return build_stack()


# --------------------------------------------------------------------------- #
# Edits
# --------------------------------------------------------------------------- #
class ScriptEdit(BaseModel):
    """One script's edits. `order` is the position in the Run All chain (0
    first); it is applied AFTER `enabled`, so a re-tick plus a position is the
    position the caller asked for."""
    enabled: Optional[bool] = None
    order: Optional[int] = None
    gate_enabled: Optional[bool] = None


class StackEdit(BaseModel):
    """Edits to the stack. Every field is optional, and each one is written to
    the config key the app already reads — `run_all_order` for the chain, the
    `grade_check_*` / `audit_*` booleans for the checks, the feature switch for
    a script's gate. `preset` writes exactly those same booleans."""
    order: Optional[List[int]] = None
    scripts: Optional[Dict[str, ScriptEdit]] = None
    checks: Optional[Dict[str, bool]] = None
    audit: Optional[Dict[str, bool]] = None
    preset: Optional[str] = None


def _unknown(what: str, ids) -> str:
    return f"unknown {what}: {', '.join(str(i) for i in ids)}"


def _script_id(raw: str) -> int:
    try:
        sid = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, _unknown("script id", [raw]))
    if sid not in RUNNERS:
        raise HTTPException(400, _unknown("script id", [sid]))
    return sid


@router.put("/api/stack")
def put_stack(edit: StackEdit):
    """Apply an edit and hand back the whole stack as it is afterwards."""
    cfg = load_config()
    order = [i for i in (cfg.get("run_all_order") or DEFAULT_RUN_ALL_ORDER)]
    writes: Dict[str, object] = {}

    if edit.order is not None:
        bad = sorted({i for i in edit.order if i not in RUNNERS})
        if bad:
            raise HTTPException(400, _unknown("script id", bad))
        seen: List[int] = []
        for i in edit.order:
            if i not in seen:
                seen.append(i)
        # Scripts left out are not dropped, they keep their default position
        # and run after the ones listed — the same completion mlo.cli's own
        # Run All editor applies before saving (normalize_config then re-anchors
        # the later ids at their canonical spots).
        order = seen + [i for i in DEFAULT_RUN_ALL_ORDER if i not in seen]
        writes["run_all_order"] = order

    for raw, sub in (edit.scripts or {}).items():
        sid = _script_id(raw)
        if sub.enabled is not None:
            gates = _gate_keys(sid)
            if gates:
                # A script's own feature switch is what makes a chain skip it,
                # so that is what "enabled" writes; switching it on also
                # restores its slot in the order (the app re-anchors the later
                # ids anyway), so a tick always means "this script runs".
                for key in gates:
                    cfg[key] = bool(sub.enabled)
                    writes[key] = bool(sub.enabled)
                if sub.enabled and sid not in order:
                    order = _with_script(order, sid, True)
                    writes["run_all_order"] = order
            elif sub.enabled or sid not in _anchored_ids():
                order = _with_script(order, sid, bool(sub.enabled))
                writes["run_all_order"] = order
            else:
                raise HTTPException(
                    400, f"script {sid} has no feature switch, and the app "
                         f"re-anchors it into the Run All order on every load "
                         f"— it cannot be switched off by dropping it")
        if sub.order is not None:
            order = _move(order, sid, max(0, min(len(order) - 1, sub.order)))
            writes["run_all_order"] = order
        if sub.gate_enabled is not None:
            keys = _gate_keys(sid)
            if not keys:
                raise HTTPException(
                    400, f"script {sid} has no feature switch to set")
            for key in keys:
                cfg[key] = bool(sub.gate_enabled)
                writes[key] = bool(sub.gate_enabled)

    known = {c["key"] for c in registry()["checks"]}
    if edit.checks:
        bad = sorted(set(edit.checks) - known)
        if bad:
            raise HTTPException(400, _unknown("check", bad))
        for key, value in edit.checks.items():
            cfg[key] = bool(value)
            writes[key] = bool(value)

    if edit.audit:
        bad = sorted(set(edit.audit) - set(_audit_keys()))
        if bad:
            raise HTTPException(400, _unknown("audit step", bad))
        for key, value in edit.audit.items():
            cfg[key] = bool(value)
            writes[key] = bool(value)

    if edit.preset is not None:
        ids = [pid for pid, _, _ in PRESETS]
        if edit.preset not in ids:
            raise HTTPException(400, _unknown("preset", [edit.preset]))
        for check in registry()["checks"]:
            value = preset_value(edit.preset, check["key"], check["default"])
            cfg[check["key"]] = value
            writes[check["key"]] = value

    if "run_all_order" in writes:
        cfg["run_all_order"] = order
    if writes and not save_config(cfg):
        reason = getattr(save_config, "last_error", "") or ""
        raise HTTPException(500, f"Failed to save config"
                                 f"{(': ' + reason) if reason else ''}")
    if writes:
        # The library payload carries grading results computed from exactly
        # these toggles, and the shelf's answers depend on the script switches:
        # both caches go, so the next read reflects the stack (the same pair
        # /api/config drops after a save).
        try:
            from server import recommendations
            recommendations.invalidate()
        except Exception:
            pass
        try:
            from server import tagcache
            tagcache.invalidate_all()
        except Exception:
            pass

    return {"ok": True, "changed": sorted(writes), "stack": build_stack()}
