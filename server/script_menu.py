"""Which script a details menu may offer for WHICH entity — asked of the
registry that runs it, never typed into a menu.

MAINTAIN -> the "…" menu beside an album, a track row, an artist or a playlist
selection is generated from THIS payload. Before it existed every menu carried
its own hand-written handful of scripts (``web/src/components/TagActionsMenu``
had ten of the twenty-one, and nothing made the two lists agree), so a script
the app could run over a selection was simply absent from the menu the user
was looking at. The registry is :data:`server.script_runners.RUNNERS`; the
order, the feature switches and the force flags come from the tables that
already own them.

GET /api/script-menu  every script, its label/description (``mlo.cli.SCRIPTS``,
    the names README.md and ``web/src/lib/scripts.ts`` mirror), its slot in the
    Run All order, the group the menu shows it in, its force flag, its feature
    switch — and ``applies_to``: the ENTITY KINDS a run of it makes sense from.
    Nothing is run from here: the client hands the ids to ``/api/run``, the one
    path the Optimization page uses.

Applicability is derived from what each runner does with a selection, and the
table below is that derivation, one citation per script. Two scopes cover all
twenty-one:

  file    the runner's work unit is a FILE. Hand it the selection's own paths
          and each file gets its own complete treatment (a tag, a sidecar
          named after the file, a re-encode, a measurement of that track).
          Such a script applies from any menu that holds paths — a track row, a
          playlist selection, an album, an artist, the library.
  folder  the runner's work unit is the FOLDER (its ``.cue``/``.accurip``/
          ``.lrc`` sidecars, its cover art, its release manifest, a per-album
          measurement, the artist image beside it, the subtree's layout). An
          audio file on its own is not something it can finish, so it is
          offered where a folder is in hand — an album, an artist, the library
          — and NOT on a track row or a playlist, which hold files.

A script the table does not know is reported in ``unclassified`` and offered
everywhere: a menu that silently dropped a newly registered script is the drift
this module exists to stop, so the payload fails OPEN and
``tools/test_script_menu.py`` fails LOUD (it freezes the table below and
refuses a registry id with no scope).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter

from mlo.cli import SCRIPTS
from mlo.config import DEFAULT_RUN_ALL_ORDER, load_config
from server.script_runners import (OPT_IN_SCRIPTS, RUNNERS, _DISABLED,
                                   _FORCE_ALIASES, _FORCE_KEYS)

router = APIRouter()

# --------------------------------------------------------------------------- #
# The entity kinds a menu is mounted on
# --------------------------------------------------------------------------- #
# What each kind's menu hands a run: a track row and a playlist selection hold
# FILES; an album, an artist and the library hold FOLDERs (and the files under
# them). That split is the whole of the applicability rule below.
KINDS = ("album", "track", "artist", "playlist", "library")
_FOLDER_KINDS = ("album", "artist", "library")

# --------------------------------------------------------------------------- #
# The applicability table — id -> (scope, why)
# --------------------------------------------------------------------------- #
# Every id in RUNNERS has an entry. The "why" is the code that decides it, and
# tools/test_script_menu.py refuses a script whose reason is missing or empty —
# a scope that cannot be pointed at the runner that justifies it is a guess,
# and a guess here hides a button or offers a no-op.
SCOPES: Dict[int, str] = {
    1: "file",    # mlo/lyrics.py: per-file lyrics format + its .lrc; the
                  # MEDIA/SOURCE half is enforced over the albums the selected
                  # files sit in, so a selection always finishes what it starts.
    2: "folder",  # mlo/cue.py: _collect_targets(targets, (".cue",)) — the
                  # target IS the .cue sidecar, which lives in the album folder;
                  # an audio file never contributes anything.
    3: "file",    # mlo/flac.py: per-file lossless re-encode / conversion.
    4: "folder",  # mlo/grader.py: albums are derived from the targets and each
                  # ALBUM folder is graded; a track has no checks of its own
                  # (server/api_query.py).
    5: "folder",  # mlo/images.py: the targets are IMAGE files (cover art in the
                  # album folder) — an audio path is filtered out by extension.
    6: "file",    # mlo/audit.py: per-file AUDIT verdict; the MEDIA=CD phases
                  # read the albums the selected files belong to.
    7: "folder",  # mlo/loudness.py: _album_dirs + "Measure ONE album's
                  # ReplayGain" — TRACK and ALBUM gain are one measurement.
    8: "folder",  # mlo/autotag.py: album_dirs; the pass renames/moves whole
                  # albums and reports moved_targets for the chain.
    9: "folder",  # mlo/accurip.py: one .accurip per CD album/disc, gated on the
                  # album's MEDIA=CD.
    10: "folder", # mlo/format_all.py: the album's final pass — its audio plus
                  # the .accurip/.cue/.lrc sidecars collected from the album
                  # folders the targets sit in.
    11: "file",   # mlo/remux.py: per-file video conversion; "an explicit
                  # target list IS the user's request ('remux THESE files')".
    12: "file",   # mlo/audiometa.py: per-file key/tempo analysis and the
                  # KEY/BPM tags it writes to that file.
    13: "file",   # mlo/lyrics_fetch.py: per-file lyric fetch, written into that
                  # file and its sidecar.
    14: "folder", # server/beetscfg.py: only FOLDER targets are accepted
                  # (os.path.isdir) — beets imports album by album.
    15: "folder", # server/script_runners.py: one .mlo_expected.json per ALBUM
                  # folder, from the release the album's identity tags name.
    16: "file",   # mlo/moods.py: per-file MOOD/ENERGY from that track's audio.
    17: "file",   # mlo/lyrics_xlit.py: per-file transforms — the
                  # TRANSLITERATION/TRANSLATION tags and .romaji.lrc sidecar.
    18: "file",   # mlo/lyrics_publish.py: per-file submission of the lyrics
                  # that file already carries.
    19: "folder", # mlo/artistdata.py: the ARTIST folder's image is the subject
                  # (artist_folders resolves a target to the artist it sits in).
    20: "folder", # mlo/layout.py: the subject is the shape of a subtree; a run
                  # with targets fixes exactly those trees.
    21: "file",   # mlo/acoustid.py: per-file ACOUSTID_ID/FINGERPRINT pair.
    22: "file",   # mlo/acoustid.py: one fingerprint + one MusicBrainz recording
                  # id belong to one FILE — the selection's own paths are what
                  # the submission is about, and its skips name files.
}

# One line per script, for the report and for the test's non-empty check.
BECAUSE: Dict[int, str] = {
    1: "formats one file's lyrics (and its .lrc)",
    2: "the .cue sidecar is an album file",
    3: "re-encodes each file it is given",
    4: "grades album folders; a track has no checks of its own",
    5: "re-processes the album's image files",
    6: "writes a per-file AUDIT verdict",
    7: "measures one album's DR / ReplayGain",
    8: "tags and renames a whole album",
    9: "writes one .accurip per CD album",
    10: "the album's final pass over its audio and sidecars",
    11: "converts each video file it is given",
    12: "analyses one file's key and tempo",
    13: "fetches one file's lyrics",
    14: "beets imports album folders only",
    15: "writes one release manifest per album",
    16: "reads one file's mood and energy",
    17: "stores one file's transliteration/translation",
    18: "submits one file's lyrics",
    19: "re-fits the artist folder's image",
    20: "fixes the layout of a whole subtree",
    21: "completes one file's AcoustID pair",
    22: "submits one file's fingerprint with its MusicBrainz recording",
}

# --------------------------------------------------------------------------- #
# Grouping — the stack's own script section
# --------------------------------------------------------------------------- #
# api_stack groups CHECKS into named sections; its scripts are ONE list (the Run
# All chain, in run_all_order). The menu keeps that shape: one section, in the
# stack's order, plus the forced re-runs as their own section.
SCRIPT_GROUP = ("scripts", "Scripts")
FORCE_GROUP = ("force", "Forced re-run")

# --------------------------------------------------------------------------- #
# Labels and switches, read out of the modules that own them
# --------------------------------------------------------------------------- #
_MENU = {sid: (name, desc) for sid, name, desc in SCRIPTS}

# config force key -> the short UI key /api/run accepts (_FORCE_ALIASES is the
# one place that mapping is stated; force.ts sends the short spelling).
_ALIAS_OF = {cfg: alias for alias, cfg in _FORCE_ALIASES.items()}

# The canonical owner of each force flag: the first script whose _FORCE_KEYS
# names it. A flag two scripts share (force_lyrics is 1's formatter and 13's
# fetcher) is labelled for the PASS it re-runs, so an option on any script says
# what pressing it does.
_OWNER_OF: Dict[str, int] = {}
for _owner_sid in sorted(_FORCE_KEYS):
    for _owner_key in _FORCE_KEYS[_owner_sid]:
        _OWNER_OF.setdefault(_owner_key, _owner_sid)


def _apply_kinds(sid: int, scopes: Optional[Dict[int, str]] = None) -> List[str]:
    """The kinds that may offer *sid*, from its scope."""
    scope = (SCOPES if scopes is None else scopes).get(sid)
    if scope == "file":
        return list(KINDS)
    if scope == "folder":
        return [k for k in KINDS if k in _FOLDER_KINDS]
    # Unknown scope (a registry id with no table entry): offer it everywhere
    # and let the test complain. Hiding it would be the drift this stops.
    return list(KINDS)


def _force(sid: int) -> dict:
    """The force options a forced re-run of *sid* may send through /api/run.

    ONE OPTION PER FLAG, always: a script whose flag is a composite (10 re-runs
    what the flags above it force) exposes each of them, labelled with the pass
    it re-runs, so a force option the registry knows is never unreachable from
    the menu — and never silently offered only for the scripts that happen to
    own exactly one.""" 
    options: List[dict] = []
    for key in _FORCE_KEYS.get(sid, ()):
        alias = _ALIAS_OF.get(key)
        if alias is None:
            # A flag with no short spelling /api/run accepts: reported, so a
            # caller can see it exists rather than finding it missing.
            options.append({"key": None, "config": key, "owner": None,
                            "owner_label": key})
            continue
        owner = _OWNER_OF.get(key)
        name = _MENU.get(owner, ("", ""))[0] if owner is not None else ""
        options.append({
            "key": alias,
            "config": key,
            "owner": owner,
            "owner_label": f"{owner} · {name}" if owner is not None and name else alias,
        })
    return {"keys": [o["key"] for o in options if o["key"]], "options": options}


def _run_all(order: List[int], scripts: List[dict]) -> dict:
    """The ids a "run all of these" press posts, ONE LIST PER ENTITY KIND.

    The chain's own order (`run_all_order`), scoped to what the entity's menu
    offers, minus the opt-in scripts — whose work is outward-facing
    (``script_runners.OPT_IN_SCRIPTS``: 22 publishes a fingerprint and a
    recording id to AcoustID's public database), so no menu button ever sweeps
    one up, not even for a user who ticked it into their own order. The count a
    client prints is ``len(list)``, so a label and the request it stands for
    cannot disagree."""
    applies = {s["id"]: s["applies_to"] for s in scripts}
    labels = {s["id"]: s["label"] for s in scripts}
    chain = [sid for sid in order
             if sid in applies and sid not in OPT_IN_SCRIPTS]
    return {
        "order": chain,
        "by_kind": {kind: [sid for sid in chain if kind in applies[sid]]
                    for kind in KINDS},
        "excluded": [{"id": sid, "label": labels.get(sid, str(sid)),
                      "why": "opt-in: its work is outward-facing"}
                     for sid in sorted(OPT_IN_SCRIPTS) if sid in applies],
    }


def _gate(sid: int, cfg: dict) -> dict:
    """The feature switch a run of *sid* skips on — and the run's own sentence
    for why, word for word the one ``run_script`` returns as ``reason``.

    The rule is the RUNNER's (``script_runners._DISABLED``): a tuple means any
    of the switches keeps it alive (17 transliterates, translates, or both)."""
    gate = _DISABLED.get(sid)
    if not gate:
        return {"keys": [], "enabled": True, "reason": ""}
    keys = list(gate if isinstance(gate, tuple) else (gate,))
    if any(bool(cfg.get(k, True)) for k in keys):
        return {"keys": keys, "enabled": True, "reason": ""}
    joined = " and ".join(keys)
    return {"keys": keys, "enabled": False,
            "reason": f"{joined} {'are' if len(keys) > 1 else 'is'} off"}


def _entry(sid: int, cfg: dict, order: List[int], runners,
           scopes: Optional[Dict[int, str]] = None) -> dict:
    label, runner = runners[sid]
    name, desc = _MENU.get(sid, (label, ""))
    return {
        "id": sid,
        "label": name,
        "description": desc,
        "group": SCRIPT_GROUP[0],
        "order": order.index(sid) if sid in order else None,
        "in_order": sid in order,
        "scope": (SCOPES if scopes is None else scopes).get(sid),
        "applies_to": _apply_kinds(sid, scopes),
        "force": _force(sid),
        "gate": _gate(sid, cfg),
        # False when this install has no such runner (a stripped checkout):
        # the menu shows it, disabled, rather than pretending it is not there.
        "available": bool(runner),
    }


def script_menu(cfg: Optional[dict] = None, runners=None,
                scopes: Optional[Dict[int, str]] = None) -> dict:
    """The payload.

    *runners* and *scopes* are injectable so a test can hand over a registry
    with an extra script and see what the endpoint does with it; the defaults
    are the real ones."""
    cfg = load_config() if cfg is None else cfg
    runners = RUNNERS if runners is None else runners
    scopes = SCOPES if scopes is None else scopes
    order = [i for i in (cfg.get("run_all_order") or DEFAULT_RUN_ALL_ORDER)]
    ids = sorted(runners)
    scripts = []
    for sid in ids:
        if sid not in (scopes or {}):
            scripts.append({
                "id": sid,
                "label": runners[sid][0],
                "description": _MENU.get(sid, ("", ""))[1],
                "group": SCRIPT_GROUP[0],
                "order": order.index(sid) if sid in order else None,
                "in_order": sid in order,
                "scope": None,
                "applies_to": list(KINDS),
                "force": _force(sid),
                "gate": _gate(sid, cfg),
                "available": bool(runners[sid][1]),
            })
            continue
        scripts.append(_entry(sid, cfg, order, runners, scopes))
    # In the stack's own order: the scripts that hold a slot first, in it, then
    # whatever is not in the chain (a client still gets one entry per script).
    scripts.sort(key=lambda s: (s["order"] is None,
                                 s["order"] if s["order"] is not None else s["id"]))
    unclassified = [s["id"] for s in scripts if s["scope"] is None]
    return {
        "kinds": list(KINDS),
        "groups": [{"id": SCRIPT_GROUP[0], "title": SCRIPT_GROUP[1]}],
        # The forced re-runs are a VARIANT of the entries above, not a script
        # group of their own: the client renders them as one more section after
        # the groups, so the menu never offers two entries for one plain run.
        "forced_group": {"id": FORCE_GROUP[0], "title": FORCE_GROUP[1]},
        # What a "run everything that applies" press posts, per entity kind —
        # the chain's order, scoped to the entity, without the opt-in scripts.
        "run_all": _run_all(order, scripts),
        "scripts": scripts,
        # Loud, for a client and for a reader of the API: these ids have no
        # applicability entry and are offered everywhere until one is written.
        "unclassified": unclassified,
    }


@router.get("/api/script-menu")
def get_script_menu():
    """Every script with the entity kinds a run of it makes sense from, the
    order the stack runs them in, the feature switch that skips it and its
    force flag. What a details menu renders, so no menu keeps its own list."""
    return script_menu()
