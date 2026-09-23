"""Saved export configurations: the Export page's form, under a name.

An export form is a dozen values a user tunes once and reuses ("my DAP", "the
car stick", "phones with the EQ"), and the app already keeps ONE set of them as
the saved defaults (`export_*` config keys). Names are the missing half: a zip
for a phone, a FLAC tree on a card and an MP3 stick are three different recipes,
not three edits of one.

The mechanism is the imported EQ profiles' own (``mlo.eq``): a named user item
is ONE file under the app's data dir, named by the same name→segment rule
(``mlo.paths.slug_name``), so a save is one small write, a delete is one unlink,
and two names can never collide inside a shared blob. The file is JSON, because
a config is a record of values rather than a curve; it lives in
``<music>/.mlo/data/export_configs/<slug>.json``, beside the ``eq`` folder of
the profiles it can name.

WHAT IS IN A CONFIG, and what is deliberately not:

* In: every field the Export page's form carries that decides what the export
  IS — destination (drive and subfolder), codec and quality, folder structure
  (including a custom script), artwork/tag options, ID3 version, ReplayGain
  mode, the files written beside the audio, verification, workers, sync mode,
  and the equalizer profile **by id**, so a load points at the profile itself
  rather than at a copy of its curve. ``source_kind`` — the page's own source
  tab — is kept as an opaque short string for whichever surface has tabs.
* Not in: the SELECTION (which playlist, which albums/artists/tracks are
  ticked). That is data, not configuration: a config carrying album paths would
  break the moment the library moved or an album was re-imported, and the same
  config would then export something else. The page's filter box is a view aid
  and is not kept either.

A saved config is exactly the shape a run takes, so a loaded one can be posted
to ``/api/export`` unchanged. Its keys are whitelisted against the exporter's
own tables (``exporter.FORM_FIELDS`` + ``exporter.EXPORT_DEFAULTS``), and the
enumerated values a run would REFUSE (an unknown codec, structure, target or
ReplayGain mode) are refused at SAVE time, while the form that produced them is
still on screen. Values a run merely NORMALISES (the subfolder, an odd quality)
are stored as typed: the run is the one authority on how they are spelled.
"""

import json
import os
import time

from mlo import eq as eq_mod
from mlo.paths import app_data_dir, safe_segment, slug_name
from server import exporter

DIRNAME = "export_configs"
SUFFIX = ".json"

# A config is a few hundred bytes. The cap is what keeps a file that is not a
# config (a library dump, a binary) from being saved under a name and read back
# as one.
MAX_CONFIG_BYTES = 64 * 1024

# A form field is short text: a drive root, a folder name, an id, a script.
MAX_TEXT_CHARS = 512

# The Export page's source tab ("playlist", "library", "albums"…). The server
# stores it verbatim — the tab list belongs to the page that has the tabs — so
# it is only checked to be short text. A surface with no tabs (the per-page
# export dialog) has no use for it.
SOURCE_KIND_FIELD = "source_kind"

# Every key a config may carry, in one place: the positional form fields minus
# `paths` (the selection is data), every run option, and the source tab above.
CONFIG_FIELDS = tuple(k for k in exporter.FORM_FIELDS if k != "paths") \
    + tuple(exporter.EXPORT_DEFAULTS) + (SOURCE_KIND_FIELD,)

# The type each field holds, from the exporter's own defaults where it has one
# (a bool stays a bool, a number a number, text text); the positional form
# fields are all text, and so is the source tab. Exact rather than permissive on
# purpose: a config file is read back as a run, and "90" would reach the
# exporter where 90 belongs.
_TYPES = {k: (type(exporter.EXPORT_DEFAULTS[k]) if k in exporter.EXPORT_DEFAULTS
              else str)
          for k in CONFIG_FIELDS}


def configs_dir(music_folder=None):
    """The folder holding saved configs (created on demand)."""
    return os.path.join(app_data_dir(music_folder), DIRNAME)


def _config_path(music_folder, stem):
    return os.path.join(configs_dir(music_folder), stem + SUFFIX)


def _saved_at(path):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return ""


def _type_sentence(key, want):
    """How a wrong-typed field is refused, in the user's terms."""
    if want is bool:
        return f"{key} must be true or false"
    if want is int:
        return f"{key} must be a whole number"
    if want is list:
        return f"{key} must be a list of file family names"
    return f"{key} must be text"


def clean_config(config):
    """The form values a config may hold, checked against the exporter's own
    tables. Raises ValueError with a sentence the UI can show.

    Refused: a key the form does not have (a config file is not a place to
    smuggle anything into a run), a value of the wrong type, and an enumerated
    value the run itself would refuse — an unknown codec, folder structure,
    target, ReplayGain mode or file family (an EMPTY file selection included),
    and an equalizer profile id that could name a file outside the profile
    folder.
    """
    if not isinstance(config, dict):
        raise ValueError("a config must be an object of the export form's fields")
    unknown = sorted(str(k) for k in config if k not in CONFIG_FIELDS)
    if unknown:
        raise ValueError("unknown config field(s): " + ", ".join(unknown))
    out = {}
    for key in CONFIG_FIELDS:
        value = config.get(key)
        if value is None:
            continue          # a field the caller left out keeps the run's default
        want = _TYPES[key]
        if type(value) is not want:
            raise ValueError(_type_sentence(key, want))
        if want is str and len(value) > MAX_TEXT_CHARS:
            raise ValueError(f"{key} is longer than {MAX_TEXT_CHARS} characters")
        out[key] = value
    codec = out.get("codec")
    if codec and codec not in exporter.CODECS:
        raise ValueError(f"unknown codec: {codec}")
    target = out.get("target")
    if target and target not in exporter.TARGETS:
        raise ValueError("unknown export target: %s — the targets are %s"
                         % (target, ", ".join(exporter.TARGETS)))
    mode = out.get("replaygain_mode")
    if mode and mode not in exporter.REPLAYGAIN_MODES:
        raise ValueError("unknown ReplayGain mode: %s — the modes are %s"
                         % (mode, ", ".join(exporter.REPLAYGAIN_MODES)))
    structure = out.get("structure")
    if structure:
        # The run's own validator and its own sentence: a structure a run would
        # refuse must not be saveable in the first place.
        problem = exporter.structure_error(structure, out.get("structure_script", ""))
        if problem:
            raise ValueError(problem)
    files = out.get("copy_files")
    if files is not None:
        # The run's own validator and its own sentence, exactly like the
        # structure above: a config the page saves must not be a way to store a
        # selection an export would refuse (an unknown family, or none at all).
        problem = exporter.copy_files_error(files)
        if problem:
            raise ValueError(problem)
    eq_id = out.get("eq_profile")
    if eq_id and safe_segment(eq_id, "equalizer profile id")[1]:
        raise ValueError("invalid equalizer profile id: %r — a profile id is a "
                         "name, not a path" % eq_id)
    return out


def _eq_state(music_folder, eq_id):
    """``(missing, problem)`` for the equalizer profile a config names.

    A config can outlive the profile it was saved with — the user deleted or
    renamed it, or the config arrived from another library — and that is the one
    part of a config that goes stale. Both answers come from the profile store
    itself (``mlo.eq``), so the load, the list and the run agree about what a
    missing or unreadable profile is.
    """
    if not eq_id:
        return False, ""
    profile = eq_mod.find(music_folder, eq_id)
    if profile is None:
        return True, (f"its equalizer profile {eq_id!r} is gone — pick another "
                      "profile before exporting")
    if profile.get("errors"):
        return False, (f"its equalizer profile {eq_id!r} cannot be read — "
                       f"{profile['errors'][0]}")
    return False, ""


def _row(music_folder, path, stem, data):
    """One saved config as the API hands it out: its identity, the form values
    a run takes, and the state of the equalizer profile it names."""
    config = data.get("config") or {}
    eq_id = str(config.get("eq_profile") or "")
    missing, problem = _eq_state(music_folder, eq_id)
    return {
        "id": stem,
        "name": str(data.get("name") or stem),
        "saved_at": _saved_at(path),
        "config": config,
        "eq_profile": eq_id,
        "eq_missing": missing,
        "eq_problem": problem,
    }


def _read(path):
    """One config file as its stored record, or None when it is not one."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.loads(f.read(MAX_CONFIG_BYTES + 1))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("config"), dict):
        return None
    return data


def list_configs(music_folder):
    """Every saved config, newest first; files that are not configs skipped."""
    folder = configs_dir(music_folder)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    rows = []
    for name in names:
        if not name.lower().endswith(SUFFIX):
            continue
        path = os.path.join(folder, name)
        data = _read(path)
        if data is None:
            continue        # not a saved config; there is nothing to report
        rows.append(_row(music_folder, path, name[:-len(SUFFIX)], data))
    rows.sort(key=lambda r: (r.get("saved_at") or "", r["id"]), reverse=True)
    return rows


def load(music_folder, config_id):
    """The saved config *config_id* names, or None when there is no such config.

    The row carries the form values under ``config`` (ready to post to
    ``/api/export``) and the state of the equalizer profile it names, so the
    caller can say "that profile is gone" instead of loading a config that
    would export a different curve, or none.
    """
    stem, error = safe_segment(config_id, "config id")
    if error:
        raise ValueError(error)
    path = _config_path(music_folder, stem)
    data = _read(path)
    if data is None:
        return None
    return _row(music_folder, path, stem, data)


def save(music_folder, name, config):
    """Store *config* under *name* and return its row.

    Saving again under an existing name REPLACES that config, which is what
    saving the one you just loaded means; the row says so through ``replaced``
    so the UI can say "updated" instead of "saved". Raises ValueError for a name
    that is not a plain name, or a config the run would refuse (see
    ``clean_config``).
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("a config name is required")
    if any(sep in raw for sep in ("/", "\\")) or ".." in raw or "\x00" in raw:
        raise ValueError(f"invalid config name: {raw!r} — a name cannot contain a path")
    stem = slug_name(raw)
    if not stem:
        raise ValueError(f"invalid config name: {raw!r}")
    cleaned = clean_config(config)
    body = json.dumps({"name": raw, "config": cleaned}, indent=2,
                      sort_keys=True, ensure_ascii=False) + "\n"
    if len(body.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("this config is too large to save")
    path = _config_path(music_folder, stem)
    replaced = os.path.isfile(path)
    try:
        os.makedirs(configs_dir(music_folder), exist_ok=True)
        # Written beside the target and moved into place, so a save that dies
        # halfway cannot leave a config the loader reads back as truncated JSON.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.replace(tmp, path)
    except OSError as e:
        raise ValueError(f"could not save the config: {e}")
    row = _row(music_folder, path, stem, json.loads(body))
    row["replaced"] = replaced
    return row


def delete(music_folder, config_id):
    """Drop one saved config. False when there is no such config."""
    stem, error = safe_segment(config_id, "config id")
    if error:
        raise ValueError(error)
    try:
        os.remove(_config_path(music_folder, stem))
        return True
    except OSError:
        return False
