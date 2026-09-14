#!/usr/bin/env python3
"""Regression: .data migration must point config.json at the CURRENT music
folder, and a folder change must carry the old .data state forward.

Run:  python tools/test_config_migration.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mlo
import mlo.config as cfgmod
import mlo.paths as pathmod

OUT = os.environ.pop("MLO_MUSIC_FOLDER", None)  # keep the test hermetic

tmp = tempfile.mkdtemp(prefix="mlo_cfgtest_")
old = os.path.join(tmp, "OldMusic").replace("\\", "/")
new = os.path.join(tmp, "NewMusic").replace("\\", "/")
os.makedirs(old)
os.makedirs(new)

real_script_dir = pathmod.SCRIPT_DIR
real_config_file = cfgmod.CONFIG_FILE
_orig_stub = None
try:
    with open(real_config_file, encoding="utf-8") as f:
        _orig_stub = f.read()
except Exception:
    pass


def write_stub(folder):
    with open(real_config_file, "w", encoding="utf-8") as f:
        json.dump({"music_folder": folder}, f, indent=2)
        f.write("\n")


def migrate():
    cfgmod._MIGRATED = False
    cfgmod._migrate_to_data_dir()


def read_cfg(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


try:
    # Scenario 1: carried-over config points at an old folder -> self-heal.
    write_stub(old)
    old_cfg = os.path.join(old, "Data", "config.json")
    os.makedirs(os.path.dirname(old_cfg))
    with open(old_cfg, "w", encoding="utf-8") as f:
        json.dump({"music_folder": old, "seed": 1}, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(old, "Data", "playlists.db"), "wb") as f:
        f.write(b"x")
    os.environ["MLO_MUSIC_FOLDER"] = new
    migrate()
    new_cfg = os.path.join(new, "Data", "config.json")
    assert os.path.isfile(new_cfg), "config not migrated to the new folder"
    assert read_cfg(new_cfg)["music_folder"] == new, \
        f"inner folder stale: {read_cfg(new_cfg)['music_folder']}"
    assert read_cfg(new_cfg)["seed"] == 1, "carried config lost fields"
    assert os.path.isfile(os.path.join(new, "Data", "playlists.db")), \
        "state files not carried forward"
    assert read_cfg(real_config_file)["music_folder"] == new, "stub not refreshed"
    shutil.rmtree(os.path.join(new, "Data"))

    # Scenario 2: fresh first migration from a legacy server/data.
    write_stub(old)
    legacy = os.path.join(os.path.dirname(real_config_file), "server", "data")
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(legacy, "legacy.db"), "wb") as f:
        f.write(b"y")
    migrate()
    new_cfg = os.path.join(new, "Data", "config.json")
    assert read_cfg(new_cfg)["music_folder"] == new
    assert os.path.isfile(os.path.join(new, "Data", "legacy.db"))
    assert read_cfg(real_config_file)["music_folder"] == new
    os.remove(os.path.join(new, "Data", "legacy.db"))

    # Scenario 3: idempotent on repeat (config already aligned).
    write_stub(new)
    with open(new_cfg, "w", encoding="utf-8") as f:
        json.dump({"music_folder": new, "seed": 3}, f, indent=2, sort_keys=True)
        f.write("\n")
    migrate()
    assert read_cfg(new_cfg)["music_folder"] == new and read_cfg(new_cfg)["seed"] == 3

    # Scenario 4: changing the music folder through save_config carries the
    # state along and keeps the new value (it must not be clobbered by the
    # migration's own alignment pass).
    write_stub(old)
    old_data = os.path.join(old, "Data")
    os.makedirs(old_data, exist_ok=True)
    with open(os.path.join(old_data, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"music_folder": old, "ai_model": "old-model"}, f,
                  indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(old_data, "wishes.db"), "wb") as f:
        f.write(b"wish")
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    ok = cfgmod.save_config({"music_folder": new, "ai_model": "new-model"})
    assert ok, "save_config failed"
    assert read_cfg(new_cfg)["music_folder"] == new, \
        f"save clobbered the new folder: {read_cfg(new_cfg)['music_folder']}"
    assert read_cfg(new_cfg)["ai_model"] == "new-model", \
        f"saved values lost: {read_cfg(new_cfg).get('ai_model')}"
    assert os.path.isfile(os.path.join(new, "Data", "wishes.db")), \
        "wishes.db did not follow the folder change"
    assert read_cfg(real_config_file)["music_folder"] == new, "stub not retargeted"

    print("PASS  all config-migration scenarios")
finally:
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    if OUT:
        os.environ["MLO_MUSIC_FOLDER"] = OUT
    for p in (old_cfg if "old_cfg" in dir() else "", new_cfg if "new_cfg" in dir() else ""):
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    legacy = os.path.join(os.path.dirname(real_config_file), "server", "data")
    for name in ("legacy.db",):
        p = os.path.join(legacy, name)
        if os.path.isfile(p):
            os.remove(p)
    if _orig_stub is not None:
        with open(real_config_file, "w", encoding="utf-8") as f:
            f.write(_orig_stub)
    cfgmod._MIGRATED = False