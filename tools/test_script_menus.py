#!/usr/bin/env python3
"""Menu-consistency gate: every surface that lists the 25 scripts must agree.

Sources checked:
  * ``EXPECTED_SCRIPTS`` below — the frozen expected registry (number + name)
  * ``server/script_runners.py`` RUNNERS — the numbers /api/run accepts
  * ``web/src/lib/scripts.ts`` SCRIPTS    — the UI's single source of truth
  * ``mlo/scripts.py``       the ONE script table (the terminal menu, the API's
    script menu and the tag registry's writer column all read it; the README
    keeps prose only, so it is no longer a table this gate can read)
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
    # writes each album's .mb_expected release tracklist.
    15: "Release tracklist",
    # 16 is the mood/energy classifier on its own — script 8 runs the same
    # code as one of its stages.
    16: "Mood & Energy",
    # 17 is the AI pass: the lyric transliteration / translation script the
    # no-AI core removed, back under a fresh id (15 stayed with the tracklist).
    17: "Lyrics transliterate (AI)",
    # 19 re-fits the artist images already in the library to the configured
    # aspect and size — the remedy for the codes grade_check_artist_image
    # raises (mlo/artistdata.py run_optimize_artist_images).
    19: "Optimize artist images",
    # 20 reports the shape of the whole music folder (mlo/layout.py), fixes
    # what it can prove and stores the report under <music>/.mlo/data, which
    # is what the Library page warns from. Apply is the DEFAULT (the scan
    # always reports; `layout_apply` off is the read-only run) — the name says
    # both halves, because "scan" alone is what the owner read as "does not
    # delete anything" (2026-10-14, R437).
    20: "Optimize library layout",
    # 21 completes an ACOUSTID_ID / ACOUSTID_FINGERPRINT pair a file only half
    # carries — the grading check that had no fixer in the registry at all
    # (mlo/acoustid.py run_fix_pairs).
    21: "Fix AcoustID pairs",
    # 22 gives back: the fingerprint + MusicBrainz recording id a file states
    # goes to AcoustID's public database (mlo/acoustid.py submit_files, the
    # same pass the import route runs). Deliberately NOT in the shipped Run All
    # order — see OPT_IN_SCRIPTS in server/script_runners.py.
    22: "Submit fingerprints (AcoustID)",
    # 23 is the tag strip script 10 already performs as a step of its own pass
    # (mlo/format_all.py excess_tags + strip_excess_tags), on its own and
    # scoped: the excess-tag/alias grades name it as the fixer, the album and
    # track menus offer it, and an import runs it with the rest of the chain.
    # It only DELETES — a file with nothing excess is not written at all.
    23: "Optimize tags",
    # 24 fills the four web-rating tags (mlo/web_ratings.py): the album's
    # public score on every track and each track's own, from the configured
    # sources. Album-scoped, fill-only, and gated on `web_ratings_enabled`.
    24: "Web ratings",
    # 25/26 fetch the artist art the import's metadata step fills, over a
    # library that arrived another way. Two scripts because the two features
    # are separately switchable (artist_image_enabled / *_enabled): a user may
    # want one and not the other. Present-only; the force flag is the OVERWRITE.
    25: "Fetch artist images",
    26: "Fetch artist descriptions",
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


def opt_in_scripts():
    """The ids the shipped Run All order deliberately does not carry, from BOTH
    halves: ``server.script_runners.OPT_IN_SCRIPTS`` (what the chain and the
    stack page read) and the ``OPT_IN_SCRIPTS`` the web registry mirrors (what
    the Settings grid offers them from). A declared exception, not a forgotten
    script: the checks in main() require every other id to hold a slot."""
    server = re.search(r"OPT_IN_SCRIPTS[^=]*= frozenset\(\{([^}]*)\}\)",
                       read("server/script_runners.py"))
    web = re.search(r"OPT_IN_SCRIPTS[^=]*=\s*\[([^\]]*)\]",
                    read("web/src/lib/scripts.ts"))
    return ({int(n) for n in re.findall(r"\d+", server.group(1))} if server else set(),
            {int(n) for n in re.findall(r"\d+", web.group(1))} if web else set())


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
    # The wizard's Finish step runs the IMPORT CHAIN, not the library Run All
    # order: the ids come from the chain preview (`imports.chain_for`, i.e.
    # `import_scripts` or DEFAULT_CHAIN), the same list every import path runs.
    # `check_wizard_finish_list` below holds that, and
    # tools/check_import_minimum.mjs renders the step and reads the ticked
    # boxes.
    "web/src/pages/ImportWizard.tsx": "importScriptsPreview",
    "web/src/pages/LibraryPage.tsx": "run_all_order",
    "web/src/pages/OptimizationPage.tsx": "run_all_order",
    "web/src/pages/SettingsPage.tsx": "run_all_order",
    # The first-run setup wizard is deliberately NOT here: its cut left it with
    # no script surface at all (it asks for the folder, the account, the tools
    # and the keys, and the import chain's own ids live in
    # the import wizard, declared above). A first-run knob for the Run All
    # order was one of the 13 steps the owner asked to drop — Settings still
    # renders it (`run_all_order`, above).
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
    top = max(EXPECTED_SCRIPTS)
    for m in ID_LIST.finditer(without_comments(rel)):
        ids = [int(n) for n in re.findall(r"\d+", m.group(0))]
        if len(ids) >= 3 and len(set(ids)) == len(ids) and all(1 <= n <= top for n in ids):
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


def check_wizard_finish_list(check):
    """The wizard's Finish list IS the import chain, not the library Run All.

    The step used to read `cfg.run_all_order` — the order the Optimization page
    and the library's Run All run — so the album being finished got a wider,
    differently ordered list than every other import path (the bulk queue
    and the wizard's own "Run the import chain" button, all of
    them `imports.chain_for`). It now takes its ids from the chain preview, so
    `import_scripts` — or DEFAULT_CHAIN — decides what Finish runs; an empty
    chain (`import_auto_scripts` off) runs nothing, exactly as an import does.

    The rendered half of this is tools/check_import_minimum.mjs, which renders
    this step against a previewed chain and reads the ticked boxes; this is the
    half that holds the SOURCE to it."""
    src = without_comments("web/src/pages/ImportWizard.tsx")
    check("the wizard's Finish list comes from the import chain preview",
          "importScriptsPreview" in src)
    check("the wizard's Finish list is no longer the Run All order",
          "run_all_order" not in src)
    # ONE list, two senders: Finish itself and the button beside it both hand
    # the runner `runAfterImportIds`, so they cannot drift apart again.
    senders = re.findall(r"api\.run\(([^,]*),", src)
    check("Finish and its button send the same list through /api/run",
          senders == ["runAfterImportIds", "runAfterImportIds"], str(senders))


def table_scripts():
    """The 25 ids `mlo/scripts.py` declares, read from the FILE.

    It is the one table the menus print (mlo.cli re-exports it, server/api_stack
    and server/script_menu include it, tags_registry's writer column names it),
    so a script that goes missing there is caught here even when every importer
    silently keeps working.
    """
    src = read("mlo/scripts.py")
    return {int(m.group(1)) for m in re.finditer(r"^\s*\((\d+), \"", src, re.M)}


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


def force_settings_keys():
    """The config keys in SettingsPage's master `FORCE_KEYS` list.

    That list drives the page's "force everything" master toggle, so it has to
    cover every force switch the app offers — a key missing here is one the
    master toggle silently cannot turn on or off."""
    src = read("web/src/pages/SettingsPage.tsx")
    block = re.search(r"const FORCE_KEYS[^=]*=\s*\[(.*?)\n  \];", src, re.S)
    return [m.group(1) for m in re.finditer(r'\{\s*k:\s*"(\w+)"', block.group(1))] if block else []


def force_callers(web_files):
    """Every force dict a UI caller sends must name a key the server accepts.

    The four "Force …" actions in `TagActionsMenu` shipped CONFIG-key names
    (`{force_audit: true}`) while `_apply_force` accepts only the short UI keys
    and script ids. An unknown key is not merely ignored: the authoritative
    pass CLEARS every flag it does not name, so those actions ran with the very
    force they asked for turned off — "Force re-audit" re-audited nothing.
    A config-key spelling in a call is therefore a failure, not a style
    question, and the short keys must actually be the ones in use.
    """
    mapped = force_keys()[1]
    bad_keys, bad_spellings = [], []
    for path in web_files:
        src = read(path)
        # `{ force_x: true }` in a call: the config-key spelling that was
        # silently dropped. `force_*` inside SettingsPage's own key LISTS is
        # legitimate (those ARE config keys, saved, not sent to /api/run).
        for name in re.findall(r"\{\s*(force_\w+)\s*:\s*(?:true|false)", src):
            bad_spellings.append(f"{path}: {name}")
        for call in re.finditer(r"api\.run\(([^;]*?)\)", src, re.S):
            for key in re.findall(r"\{\s*(\w+)\s*:\s*(?:true|false)", call.group(1)):
                if key not in mapped:
                    bad_keys.append(f"{path}: {key}")
    return bad_keys, bad_spellings


def force_bare_bool_covers_a_chain():
    """A bare True/False must cover the WHOLE chain, not nothing.

    `_run_chain_locked` applies force once with `sid=None`; the bool branch
    used to look up `_FORCE_KEYS.get(None, ())`, which is empty, so a
    `run_chain(..., force=True)` forced nothing while reporting the run as
    forced."""
    body = re.search(r"if isinstance\(force, bool\):(.*?)\n    return", read("server/script_runners.py"), re.S)
    block = body.group(1) if body else ""
    return bool(block) and "_FORCE_KEYS.values() if sid is None" in block


def check_apply_force(check):
    """The force contract, EXECUTED rather than read.

    The static checks above can see the shape of `_apply_force`; these pin what
    it does: a supplied dict is authoritative and complete (so an unchecked
    script cannot stay forced by a saved switch), both spellings the UI may use
    land on the same config flag, a config-key spelling does NOT (it is dropped,
    and the clear leaves the flag off — the bug the Re-run & overwrite menu
    shipped), a bare bool covers a whole chain, and None changes nothing."""
    sys.path.insert(0, ROOT)
    from server import script_runners as sr

    def fresh():
        cfg = {key: False for keys in sr._FORCE_KEYS.values() for key in keys}
        cfg["layout_apply"] = True  # the one flag whose saved value is ON
        return cfg

    def apply(force, sid=None):
        cfg = fresh()
        sr._apply_force(cfg, force, sid)
        return cfg

    keys = sorted({k for v in sr._FORCE_KEYS.values() for k in v})
    off = apply(None)
    check("force=None leaves the saved flags alone",
          all(off[k] is False for k in keys if k != "layout_apply") and off["layout_apply"] is True,
          str(off))
    empty = apply({})
    check("an empty force dict clears every flag, layout_apply included",
          not any(empty.values()), str(empty))
    short = apply({"flac": True})
    check("a short UI key sets exactly its own flag",
          short["force_reencode_flac"] is True and sum(short.values()) == 1, str(short))
    by_id = apply({"3": True})
    check("a script id sets the same flag as its short name",
          by_id == short, f"id={by_id}")
    spelled = apply({"force_reencode_flac": True})
    check("a config-key spelling is dropped, not honoured",
          not any(spelled.values()), str(spelled))
    whole = apply(True)
    check("a bare True covers the whole chain",
          all(whole[k] is True for k in keys), str(whole))
    none_whole = apply(False)
    check("a bare False clears the whole chain",
          not any(none_whole.values()), str(none_whole))
    scoped = apply({"audit": True}, sid=5)
    check("a single-script application only touches that script's flags",
          scoped["force_audit"] is False, str(scoped))
    scoped6 = apply({"audit": True}, sid=6)
    check("...and applies it for the script that owns it",
          scoped6["force_audit"] is True, str(scoped6))
    # Script 24 joined the force tables late (it was config-only): pin its own
    # key, both spellings, and that the authoritative clear reaches it.
    check("script 24 maps to force_web_ratings in _FORCE_KEYS",
          sr._FORCE_KEYS.get(24) == ("force_web_ratings",),
          str(sr._FORCE_KEYS.get(24)))
    web = apply({"web_ratings": True})
    check("the web_ratings alias sets force_web_ratings",
          web["force_web_ratings"] is True and sum(web.values()) == 1, str(web))
    web_by_id = apply({"24": True})
    check("script id 24 sets the same flag as the web_ratings alias",
          web_by_id == web, f"id={web_by_id}")
    cleared = apply({})
    check("an empty dict clears force_web_ratings first",
          cleared["force_web_ratings"] is False, str(cleared))
    # Scripts 25/26 joined the same way: pin their aliases, their ids, and that
    # the authoritative clear reaches the artist fetch flags too.
    img = apply({"artist_image": True})
    check("the artist_image alias sets force_artist_image",
          img["force_artist_image"] is True and sum(img.values()) == 1, str(img))
    img_by_id = apply({"25": True})
    check("script id 25 sets the same flag as the artist_image alias",
          img_by_id == img, f"id={img_by_id}")
    desc = apply({"artist_description": True})
    check("the artist_description alias sets force_artist_description",
          desc["force_artist_description"] is True and sum(desc.values()) == 1, str(desc))
    desc_by_id = apply({"26": True})
    check("script id 26 sets the same flag as the artist_description alias",
          desc_by_id == desc, f"id={desc_by_id}")
    check("an empty dict clears the artist fetch flags first",
          apply({})["force_artist_image"] is False
          and apply({})["force_artist_description"] is False, str(apply({})))


def import_paths_do_not_send_an_empty_force():
    """No import entry point may send `{}` as its force dict.

    A supplied dict is authoritative and complete, so `{}` means "every force
    flag OFF" — `layout_apply` included, which left the import chain's layout
    pass a read-only report on the album it had just imported. The bulk queue
    passes `force=None` (saved switches); the wizard's
    re-run and the row menu were the two paths that disagreed, and a defaulted
    `force = {}` parameter is how that happened."""
    src = read("web/src/api.ts")
    signature = re.search(r"importFinish:\s*\(([^)]*)\)", src)
    defaulted = bool(signature) and "= {}" in signature.group(1)
    callers = re.findall(r"importFinish\(([^)]*)\)", read("web/src/pages/ImportWizard.tsx")
                         + read("web/src/components/TagActionsMenu.tsx"))
    empties = [c for c in callers if re.search(r",\s*\{\s*\}", c)]
    return defaulted, empties


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
          sorted(got) == [i for i in range(1, 27) if i not in (18, 22)], str(sorted(got)))
    # 17 was never in a saved order before it existed; the same shed-and-anchor
    # rule has to place it after the fetch it reads from.
    check("17 (AI transforms) lands after 13 (fetch lyrics) in a normalized order",
          got.index(17) == got.index(13) + 1, str(got))
    # 20 and 21 were not in any saved order yet either: they join at their
    # anchors, which are the final pass (10) and the report on it (20).
    check("20 (layout scan) lands after 10 (format all) for an existing install",
          got.count(20) == 1 and got.index(10) < got.index(20)
          and got.index(20) < got.index(21), str(got))
    check("21 (AcoustID pairs) lands right after 20 for an existing install",
          got.count(21) == 1 and got.index(21) == got.index(20) + 1, str(got))
    # 23 (the tag strip) was in no saved order either: it joins right behind
    # 10, the pass whose excess lists it deletes by — pushing the read-outs
    # (20, 21) no further than they already were, and never after the pass
    # they come from.
    check("23 (tag hygiene) lands right behind 10 for an existing install",
          got.count(23) == 1 and got.index(23) == got.index(10) + 1
          and got.index(23) < got.index(20) and got.index(21) == got.index(20) + 1,
          str(got))
    # 24 (web ratings) joins the same way: the shipped order puts it right
    # behind 8 (Auto tagging), and an install upgrading into it is anchored
    # there rather than having the script pushed to the end of its own chain.
    check("24 (web ratings) lands right behind 8 for an existing install",
          got.count(24) == 1 and got.index(24) == got.index(8) + 1, str(got))
    # 25 (artist images) and 26 (artist descriptions) join in front of 19
    # (Optimize artist images): a saved order that predates them gets fetch,
    # then re-fit, and the two fetches keep their own order.
    check("25 (artist images) lands right before 19 for an existing install",
          got.count(25) == 1 and got.index(25) == got.index(19) - 2, str(got))
    check("26 (artist descriptions) lands right after 25 for an existing install",
          got.count(26) == 1 and got.index(26) == got.index(25) + 1, str(got))
    # ...and on a later load they are KEPT where the user put them: unlike 15,
    # their ids never meant anything else, so a saved position is a real choice
    # and re-anchoring them would silently undo the drag.
    moved = cfg.normalize_config({"music_folder": "X",
                                  "run_all_order": [sid for sid in got if sid not in (20, 21)] + [21, 20]})
    check("a saved position for 20 and 21 is kept, not re-anchored after 10",
          moved["run_all_order"][-2:] == [21, 20], str(moved["run_all_order"]))

    junk = cfg.normalize_config({"music_folder": "X", "run_all_order": [99, "a", 4, 4, -1]})
    check("unknown / duplicate run-all ids are dropped",
          all(1 <= n <= 26 for n in junk["run_all_order"]) and len(set(junk["run_all_order"])) == len(junk["run_all_order"]),
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
    # The chain's GRAPH tables (mlo/scripts.py, spec R388): a key or a dep that
    # names no script is a rule that quietly stopped applying — the lane a
    # removed script used to have, or an edge waiting on nothing.
    from mlo.scripts import SCRIPT_DEPS, SCRIPT_LANES
    check("every SCRIPT_LANES key is a real script",
          set(SCRIPT_LANES) <= runners, str(sorted(set(SCRIPT_LANES) - runners)))
    check("every SCRIPT_DEPS key and dependency names a real script (or the "
          "import's own advisory step, which is not a script)",
          set(SCRIPT_DEPS) <= runners
          and all(dep in runners or dep == "advisories"
                  for deps in SCRIPT_DEPS.values() for dep in deps),
          f"keys={sorted(set(SCRIPT_DEPS) - runners)} "
          f"deps={sorted({d for deps in SCRIPT_DEPS.values() for d in deps} - runners)}")
    check("a script is never its own dependency",
          all(sid not in deps for sid, deps in SCRIPT_DEPS.items()),
          str({sid: deps for sid, deps in SCRIPT_DEPS.items() if sid in deps}))
    check("the free lane is exactly the scripts that touch no album file",
          set(SCRIPT_LANES) == {19, 22, 25, 26}, str(sorted(SCRIPT_LANES)))
    # Advisory-after-lyrics is the owner's rule and it is an EDGE, not a
    # position: script 8 reads the advisory tags, which read the lyrics.
    check("Auto Tagging waits for the lyrics AND the advisory step",
          set(SCRIPT_DEPS.get(8, ())) >= {13, "advisories"},
          str(SCRIPT_DEPS.get(8)))


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


def check_wizard_close_and_expected(check):
    """The Finish step's Close, and the ONE place `.mb_expected` is written.

    Close runs the same settle + organize + dismiss as Done but NEVER the
    chain: `runScripts` guards the only `/api/run` call `finish` makes, and the
    Close control passes it false, so a press that means "finish by hand" cannot
    re-run the scripts. And the release's tracklist has ONE writer —
    `api.importExpected` is called from the `recordExpectedTracks` helper alone,
    which a plain step advance (`advanceTo`) and every settle of a release
    (`pickRelease`) both reach — so the folder's `.mb_expected` is (re)written
    whenever the album's release moves, not only on the Match save."""
    src = without_comments("web/src/pages/ImportWizard.tsx")
    # Both Done controls keep running the chain (the flag defaults ON)…
    check("Done keeps running the chain (a Link and a plain button)",
          "onClick={() => finish(true)}" in src
          and "onClick={() => finish(false)}" in src)
    # …and Close is its own control that never does.
    check("Close runs no script", "onClick={() => finish(false, false)}" in src)
    sig = re.search(r"const finish = async \(([^)]*)\)", src)
    check("finish takes a runScripts flag defaulting ON",
          bool(sig) and "runScripts = true" in sig.group(1),
          sig.group(1) if sig else "no finish signature")
    check("the chain call is the flag's own to make",
          "if (runScripts && runAfterImportIds.length" in src)
    # ONE `.mb_expected` writer: the helper, and it is the only caller of
    # `/api/import/expected` in the wizard.
    check("the release tracklist has ONE writer (no copied call)",
          src.count("api.importExpected(") == 1
          and "const recordExpectedTracks = async" in src
          and "const syncExpectedTracks = async" in src)
    check("a step advance records the release's tracklist",
          re.search(r"const advanceTo = \(to: number\) => \{\s*"
                    r"void syncExpectedTracks\(", src) is not None)
    check("settling a release (pickRelease) records it too",
          "await syncExpectedTracks(target ?? albumPath, rel);" in src)
    # The step list carries the family right after Genres, and the registry's
    # step NAME is the one the list holds: a prompt's link resolves by name, so
    # a renamed or shifted entry would land on a neighbour.
    check("the step list carries Ratings right after Genres",
          re.search(r'STEPS = \[[^\]]*"Genres", "Ratings",[^\]]*"Finish"\]', src) is not None)
    check("FAMILY_STEP and FAMILY_LABEL name the ratings family's step",
          src.count('ratings: "Ratings"') == 2)
    # Close is the SECONDARY control beside Done (Done is the primary press),
    # and its own toast says BOTH halves were skipped — the chain and the
    # naming/organize step (R416: the status line's "naming script" was the
    # owner's evidence that Close still ran a script).
    done_at = src.index("onClick={() => finish(true)}")
    close_at = src.index("onClick={() => finish(false, false)}")
    check("Close sits after Done and is the secondary (ghost) button",
          close_at > done_at and "btn-ghost" in src[max(0, close_at - 300):close_at])
    check("Close's toast says the naming step and the scripts were skipped",
          "Import closed — the naming step and the scripts were skipped" in src)
    # …and the press itself runs neither: the organizer's call is gated on the
    # same flag the chain is (it IS a script), and the settle Close sends
    # carries an EMPTY chain (the "no fetch is coming" signal).
    check("Close skips the organizer with the chain",
          "if (runScripts && targets.length && nameable)" in src)
    check("Close's settle carries an empty chain",
          "scripts: runScripts ? runAfterImportIds : []" in src)
    # The Ratings step runs the chain's entry point with the shared one-shot
    # Force, so a re-fetch can replace where the default fills only.
    check("the Ratings step sends the shared Force to /api/import/ratings",
          "ratingsImport(target, staged, forced)" in src
          and 'forceSel["web_ratings"]' in src)


def check_wizard_step_jumps(check):
    """R417: a step JUMP saves the step being LEFT.

    The owner's rule (2026-10-13): "for each page of importing, if the user
    goes to another tab the one they were previously on saves
    automatically". Every jump surface — the step strip, a missing-family chip
    in the "Still missing" banner, and Back — routes through ONE `goTo`, which
    runs the departing step's own save (its Continue action) before landing.
    The save is dirty-gated (`editedSteps`): a step nobody edited writes
    nothing at all, so a browse-through cannot churn files or toast a save
    that did not happen. `tools/check_import_minimum.mjs` drives the real page
    for the behaviour; this pins the wiring."""
    src = read("web/src/pages/ImportWizard.tsx")
    # Every jump surface routes through `goTo`, and it saves the step it leaves
    # BEFORE moving — a plain `setStep` on any of them is the bug this pins.
    strip = src[src.index("data-step={i}") - 400:src.index("data-step={i}") + 100]
    check("the step strip jumps through goTo", "onClick={() => goTo(i)}" in strip)
    check("a missing-family chip jumps through goTo",
          "onClick={() => i >= 0 && goTo(i)}" in src)
    check("Back jumps through goTo", "onClick={() => goTo(step - 1)}" in src)
    check("no step strip or banner jump calls setStep directly",
          "onClick={() => setStep(i)}" not in src)
    go_to = re.search(r"const goTo = \(to: number\) => \{(.*?)\n  \};", src, re.S)
    check("goTo saves the step being left, then lands",
          bool(go_to) and "saveStep(step);" in go_to.group(1)
          and "advanceTo(to);" in go_to.group(1),
          go_to.group(1) if go_to else "no goTo body")
    # The save is DIRTY-GATED: a browse-through writes nothing.
    save_step = re.search(r"const saveStep = \(n: number\): boolean => \{(.*?)\n  \};", src, re.S)
    check("saveStep writes nothing for a step nobody edited",
          bool(save_step) and "if (!editedSteps.has(n) || busy) return false;" in save_step.group(1),
          save_step.group(1) if save_step else "no saveStep body")
    check("…and nothing while another action already runs (no double write)",
          bool(save_step) and "|| busy" in save_step.group(1).split("if (!editedSteps")[1].split("\n")[0],
          save_step.group(1) if save_step else "no saveStep body")
    check("…and dispatches to the five steps that hold drafts",
          bool(save_step) and all(
              f"void {fn}(null);" in save_step.group(1)
              for fn in ("nextFromLinks", "confirmMatch", "saveGenres", "saveLyricsStep", "saveAdvisory")),
          save_step.group(1) if save_step else "no saveStep body")
    # Each of those saves takes the landing step, so a jump's background save
    # cannot move the wizard out from under the user.
    for fn in ("nextFromLinks", "confirmMatch", "saveGenres", "saveLyricsStep", "saveAdvisory"):
        sig = re.search(rf"const {fn} = async \(([^)]*)\)", src)
        check(f"{fn} takes the landing step (null = don't move)",
              bool(sig) and "land: number | null" in sig.group(1),
              sig.group(1) if sig else f"no {fn} signature")
    # The marks: a user edit marks its step, a successful save clears it — and a
    # save that FAILED leaves it pending (its catch says so).
    check("the edits are marked at the user's own actions",
          all(s in src for s in (
              "markEdited(1);", "markEdited(2);", "markEdited(4);",
              "markEdited(6);", "markEdited(7);")))
    check("…and cleared by the save that succeeded",
          all(f"markSaved({n});" in src for n in (1, 2, 4, 6, 7)))
    check("switching albums drops the marks (A's drafts never land on B)",
          src.count("setEditedSteps(new Set());") == 2)
    # The Ratings step and Covers have nothing of their own to write — their
    # absence from the dispatch is the claim.
    check("the dispatch never invents a save for Covers or Ratings",
          "saveCovers(null)" not in src and "ratingsImport(null" not in src)


def check_wizard_import_prompts(check):
    """R418/R421: the auto-import asks about every album, the queue says what
    the chain did, and a folder can be staged as the files the user kept.

    `tools/check_import_minimum.mjs` drives the first two on the real page;
    this pins the wiring that makes them possible, and the one thing a page
    check cannot reach (a native folder split into two albums)."""
    src = read("web/src/pages/ImportWizard.tsx")
    begin = re.search(r"const beginAutoImport = async \((.*?)\n  \};", src, re.S)
    # The row builder lives in its own function since R424: EVERY press that
    # queues a batch (Auto-import, the plain multi-album Import) raises the same
    # dialog, so the row-per-album rule is pinned where it now is — one row for
    # each staged album, carrying the release its files name as `current` (the
    # value the dialog compares an edit against), with no separate "unknown"
    # list for a row to hide in.
    rows_fn = re.search(r"const identifyRows = async \(.*?\n  \};", src, re.S)
    check("the identify dialog builds a row for EVERY staged album",
          bool(rows_fn) and "rows.push({" in rows_fn.group(0)
          and "current: pin," in rows_fn.group(0)
          and "unknown.push" not in rows_fn.group(0),
          rows_fn.group(0)[:200] if rows_fn else "no identifyRows")
    # R428: every album this dialog sees is staged (outside the library),
    # and /api/album REFUSES such a path unless the caller opts in — so the
    # row read must ask with the flag (without it the read failed and every
    # row opened BLANK, which is the owner's report of 2026-10-13), and the
    # row carries it on for its own AcoustID Detect.
    check("its rows read with the wizard's `staged` opt-in",
          bool(rows_fn) and "api.album(r.path, outside)" in rows_fn.group(0)
          and "inMusicFolder(r.path, cfg?.music_folder)" in rows_fn.group(0)
          and "staged: outside," in rows_fn.group(0),
          rows_fn.group(0)[:200] if rows_fn else "no identifyRows")
    check("Cancel discards what this press staged — into the trash, and "
          "only what the server said IT created",
          "discardStaged" in src and "r.owned" in src
          and "api.removeAlbum(r.path)" in src)
    check("…and always raises the dialog (no early import without it)",
          bool(begin) and "openIdentifyNow(staged, \"auto\")" in begin.group(1)
          and "startAutoImport(staged, {})" not in begin.group(1))
    check("…and does not wait to READ the rows before it appears",
          # The dialog opens on the press (`openIdentifyNow` seeds the rows
          # synchronously) and each row's release is filled in afterwards —
          # so the click handler must not await `identifyRows` first.
          "const openIdentifyNow = (" in src
          and "void identifyRows(staged).then" in src)
    check("a row left on the release it names sends no pin",
          # The row's baseline is `seedOf` — the caller's `current`, else the
          # release its own TAGS state (R433): a row left on either sends
          # nothing, and only an edited row is a pin.
          "const base = row ? seedOf(row) : \"\";" in
          read("web/src/components/ImportIdentifyDialog.tsx")
          and "if (v && v.toLowerCase() !== base.toLowerCase()) pinsOut[path] = v;" in
          read("web/src/components/ImportIdentifyDialog.tsx"))
    # The queue row carries the chain's own verdict.
    check("the queue row prints the server's line about the chain",
          "row?.note" in src and "Imported — no chain" in src)
    check("…and only a row whose chain RAN reads as a plain success",
          "state === \"imported\" && row?.chained === false" in src)
    # R421/R422: a native folder's files are selectable and carry their own
    # paths — through the ONE route an OS drop of several folders takes, which
    # the picker now hands its answer to (the relPath is prefixed with the
    # folder's own name there, so several folders stay separated).
    check("a native folder's files carry their OWN server paths",
          src.count("native: scan.file ? root :") == 1
          and "await handleDropPaths(picked);" in src)
    check("…and the folder's name leads the path, so folders never merge",
          "relPath: `${name}/${f.relPath}`," in src)
    check("…and are selectable like an upload's",
          "selectable={!!(f.file || f.staged || f.native || f.nativeRoot)}" in src)
    check("a native group is committed as-is only while it is WHOLE",
          "rootCounts.get(f.nativeRoot!) ?? 0) === g.files.length" in src)
    check("a subset is staged by its FILES (one ingest per file)",
          "onDisk.length && onDisk.length === keep.length" in src)
    check("…and a folder ingest happens only for an as-is group",
          "} else if (asIs(g) && folder) {" in src
          and "undo the split" in src)
    check("the wizard's album detection uses the library's disc vocabulary",
          "discDirNumber(baseName(d)) !== null" in src
          and "isDiscParent(siblings.map((s) => baseName(s)))" in src
          and "from \"../lib/discs\"" in src
          and "DISC_RE" not in src)


def main():
    fail = 0
    canon = EXPECTED_SCRIPTS
    runners = server_runners()
    web = web_scripts()
    table = table_scripts()
    run_all = web_default_run_all()

    def check(label, ok, detail=""):
        nonlocal fail
        print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  " + detail))
        if not ok:
            fail += 1

    print("scripts registry")
    opt_in_py, opt_in_web = opt_in_scripts()
    check("the web and the server declare the same opt-in scripts",
          opt_in_py == opt_in_web and bool(opt_in_py),
          f"server={sorted(opt_in_py)} web={sorted(opt_in_web)}")
    check("canonical registry has 25 scripts", len(canon) == 25, str(sorted(canon)))
    check("canonical numbers are 1..26 less the retired 18",
          sorted(canon) == [i for i in range(1, 27) if i != 18])
    check("every opt-in script is a real script", opt_in_py <= set(canon),
          f"unknown={sorted(opt_in_py - set(canon))}")
    check("server RUNNERS == canonical numbers", runners == set(canon), f"server={sorted(runners)}")
    check("web SCRIPTS == canonical numbers", set(web) == set(canon), f"web={sorted(web)}")
    check("mlo/scripts.py table == canonical numbers", table == set(canon), f"table={sorted(table)}")

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
    # `canon` is the shipped registry: 18 is retired, so "every script" means
    # every one it still lists, less the opt-in ones.
    shipped = sorted(set(canon) - set(opt_in_py))
    check("DEFAULT_RUN_ALL covers every script that ships in the order",
          sorted(run_all) == shipped, str(sorted(run_all)))
    check("…and leaves the opt-in scripts out of it",
          not (set(run_all) & opt_in_py), str(sorted(set(run_all) & opt_in_py)))
    check("DEFAULT_RUN_ALL has no duplicates", len(run_all) == len(set(run_all)), str(run_all))
    check("mlo/config.py DEFAULT_RUN_ALL_ORDER covers every script",
          sorted(py_run_all) == shipped, str(sorted(py_run_all)))
    check("python and web run-all order agree", py_run_all == run_all,
          f"python={py_run_all} web={run_all}")

    print("force switches")
    keys, mapped = force_keys()
    check("every force switch maps to a /api/run force key",
          keys <= mapped, f"unmapped={sorted(keys - mapped)}")
    settings_keys = force_settings_keys()
    alias_block = re.search(r"_FORCE_ALIASES = \{(.*?)\n\}", read("server/script_runners.py"), re.S).group(1)
    mapped_config = set(re.findall(r'"\w+":\s*"(\w+)"', alias_block))
    check("Settings' master force list covers every mapped force config key",
          mapped_config <= set(settings_keys),
          f"missing={sorted(mapped_config - set(settings_keys))} extra={sorted(set(settings_keys) - mapped_config)}")
    check("the Force menu offers 24 · Web ratings re-fetch",
          '{ key: "web_ratings", label: "24 · Web ratings re-fetch" }' in read("web/src/lib/force.ts"))
    check("the Settings master list offers force_web_ratings",
          '{ k: "force_web_ratings", label: "24 · Web ratings re-fetch" }' in read("web/src/pages/SettingsPage.tsx"))
    # 25/26 joined the force tables with the scripts themselves; pin both
    # spellings, both UI surfaces, and that the label says OVERWRITE ("re-fetch").
    check("the Force menu offers 25 · Artist images re-fetch",
          '{ key: "artist_image", label: "25 · Artist images re-fetch" }' in read("web/src/lib/force.ts"))
    check("the Force menu offers 26 · Artist descriptions re-fetch",
          '{ key: "artist_description", label: "26 · Artist descriptions re-fetch" }' in read("web/src/lib/force.ts"))
    check("the Settings master list offers force_artist_image",
          '{ k: "force_artist_image", label: "25 · Artist images re-fetch" }' in read("web/src/pages/SettingsPage.tsx"))
    check("the Settings master list offers force_artist_description",
          '{ k: "force_artist_description", label: "26 · Artist descriptions re-fetch" }' in read("web/src/pages/SettingsPage.tsx"))
    fallbacks, wrong = force_defaults_are_false()
    check("one-shot force treats unselected keys as off",
          not fallbacks and not wrong,
          f"falls back to saved config: {fallbacks + wrong}")
    bad_keys, bad_spellings = force_callers(web_run_callers())
    check("every force a UI caller sends is a key the server accepts",
          not bad_keys, str(bad_keys))
    check("no caller spells a force as its config key",
          not bad_spellings, str(bad_spellings))
    check("a bare force covers a whole chain",
          force_bare_bool_covers_a_chain(), "the bool branch must span sid=None")
    defaulted, empties = import_paths_do_not_send_an_empty_force()
    check("no import path sends an empty force dict",
          not defaulted and not empties,
          f"defaulted={defaulted} empty-call={empties}")
    check_apply_force(check)

    print("run-all migration")
    check_run_all_migration(check)

    print("import chain")
    check_import_chain(check)

    print("script surfaces")
    check_script_surfaces(check)

    print("wizard finish list")
    check_wizard_finish_list(check)

    print("wizard Close + release tracklist")
    check_wizard_close_and_expected(check)

    print("wizard step jumps (R417)")
    check_wizard_step_jumps(check)

    print("wizard import prompts + folder splits (R418/R421)")
    check_wizard_import_prompts(check)

    print("terminal runners")
    check_cli_runners(check)

    print(f"\n{'PASS' if not fail else 'FAIL'} — {fail} problem(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
