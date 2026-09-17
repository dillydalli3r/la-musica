#!/usr/bin/env python3
"""Regression: app state lives in <music>/.mlo/data, downloads in
<music>/.mlo/downloads and the trash bin in <music>/.mlo/trash.

Covers the move off the previous layouts (<music>/Data plus the intermediate
<music>/.mlo_data, and <music>/.mlo_downloads / <music>/.mlo_trash), the
self-heal of a carried-over config that still points at its old folder, a
music-folder change that carries state AND the transient dirs along, never
clobbering an occupied destination, and idempotency.

Everything happens against temp music folders and a temp config path: the
module-level CONFIG_FILE of mlo.config / mlo.paths and the legacy repo-local
state dir are redirected for the duration, so the real config.json — the file
the running app reads to find <music>/.mlo/data — is never opened for writing,
moved or truncated, and the repo's server/data is never touched. An interrupted
run therefore cannot leave a user's app pointing at a deleted temp dir.

Run:  python tools/test_config_migration.py
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mlo.config as cfgmod
import mlo.paths as pathmod

OUT = os.environ.pop("MLO_MUSIC_FOLDER", None)  # keep the test hermetic

tmp = tempfile.mkdtemp(prefix="mlo_cfgtest_")
stub = os.path.join(tmp, "config.json")
legacy = os.path.join(tmp, "server", "data")

real_config_files = {mod: mod.CONFIG_FILE for mod in (cfgmod, pathmod)}
real_legacy = {mod: getattr(mod, "LEGACY_DATA_DIR", None)
               for mod in (cfgmod, pathmod)}

for mod in (cfgmod, pathmod):
    mod.CONFIG_FILE = stub
    if real_legacy[mod] is not None:
        mod.LEGACY_DATA_DIR = legacy


# --------------------------------------------------------------------------- #
# the layout the tests assert on (literal: the spec, not the helpers under it)
# --------------------------------------------------------------------------- #
def state(mf):
    return os.path.join(mf, ".mlo", "data")


def downloads(mf):
    return os.path.join(mf, ".mlo", "downloads")


def legacy_mlo_data(mf):
    """The intermediate layout inside the music folder: <music>/.mlo_data is
    now only a migration source, never a destination."""
    return os.path.join(mf, ".mlo_data")


def bin_dir(mf):
    return os.path.join(mf, ".mlo", "trash")


def make_folder(name):
    p = os.path.join(tmp, name).replace("\\", "/")
    os.makedirs(p)
    return p


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)
    return path


def write_stub(folder):
    with open(stub, "w", encoding="utf-8") as f:
        json.dump({"music_folder": folder}, f, indent=2)
        f.write("\n")


def sqlite_db(path, rows=()):
    """A real SQLite database (one table, *rows* in it) — stands in for the
    app's playlists.db/wishes.db."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    try:
        with con:
            con.execute("CREATE TABLE t (v TEXT)")
            con.executemany("INSERT INTO t (v) VALUES (?)", [(r,) for r in rows])
    finally:
        con.close()
    return path


def db_rows(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT v FROM t ORDER BY v").fetchall()]
    finally:
        con.close()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def migrate():
    cfgmod._MIGRATED = False
    cfgmod._migrate_to_data_dir()


def files_under(root):
    """Every file below *root* as {relative path: sha1} — directories left
    behind empty are the migration's business, files are not."""
    out = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root).replace("\\", "/")] = \
                    hashlib.sha1(f.read()).hexdigest()
    return out


def assert_empty(root, what):
    left = files_under(root)
    assert not left, f"{what} still holds files after the move: {sorted(left)}"


try:
    # Scenario 0: the frozen helper API answers with the contract paths.
    probe = os.path.join(tmp, "Probe").replace("\\", "/")
    assert pathmod.mlo_root(probe).replace("\\", "/") == \
        os.path.join(probe, ".mlo").replace("\\", "/"), pathmod.mlo_root(probe)
    assert pathmod.app_data_dir(probe).replace("\\", "/") == \
        state(probe).replace("\\", "/"), pathmod.app_data_dir(probe)
    assert pathmod.downloads_dir(probe).replace("\\", "/") == \
        downloads(probe).replace("\\", "/"), pathmod.downloads_dir(probe)
    assert pathmod.trash_dir(probe).replace("\\", "/") == \
        bin_dir(probe).replace("\\", "/"), pathmod.trash_dir(probe)

    # ----------------------------------------------------------------------- #
    # Scenario 1: carried-over config points at the old folder -> self-heal,
    # and every state file lands in the new folder's .mlo/data.
    # ----------------------------------------------------------------------- #
    s1_old, s1_new = make_folder("S1Old"), make_folder("S1New")
    write_stub(s1_old)
    write(os.path.join(s1_old, "Data", "config.json"),
          json.dumps({"music_folder": s1_old, "seed": 1}, indent=2, sort_keys=True) + "\n")
    carried = {
        "playlists.db": b"playlists",
        "wishes.db": b"wishes",
        "beets-library.db": b"beets",
        os.path.join("lyrics_cache", "a.json"): b"{}",
    }
    for rel, data in carried.items():
        write(os.path.join(s1_old, "Data", rel), data)
    os.environ["MLO_MUSIC_FOLDER"] = s1_new
    migrate()
    s1_cfg = os.path.join(state(s1_new), "config.json")
    assert os.path.isfile(s1_cfg), "config not migrated to the new folder"
    assert read_json(s1_cfg)["music_folder"] == s1_new, \
        f"inner folder stale: {read_json(s1_cfg)['music_folder']}"
    assert read_json(s1_cfg)["seed"] == 1, "carried config lost fields"
    for rel, data in carried.items():
        p = os.path.join(state(s1_new), rel)
        assert os.path.isfile(p), f"{rel} did not follow the folder change"
        with open(p, "rb") as f:
            assert f.read() == data, f"{rel} arrived altered"
    assert read_json(stub)["music_folder"] == s1_new, "stub not refreshed"
    os.environ.pop("MLO_MUSIC_FOLDER", None)

    # ----------------------------------------------------------------------- #
    # Scenario 2: fresh first migration out of the legacy repo-local dir.
    # ----------------------------------------------------------------------- #
    s2 = make_folder("S2Music")
    write_stub(s2)
    write(os.path.join(legacy, "legacy.db"), b"legacy")
    migrate()
    assert os.path.isfile(os.path.join(state(s2), "legacy.db")), \
        "legacy state did not reach .mlo/data"
    assert not os.path.exists(os.path.join(legacy, "legacy.db")), \
        "legacy state was copied and left behind"

    # ----------------------------------------------------------------------- #
    # Scenario 3: migration off the PREVIOUS layout — <mf>/Data plus the
    # .mlo_downloads and .mlo_trash dirs — into the new locations.
    # ----------------------------------------------------------------------- #
    s3 = make_folder("S3Music")
    write_stub(s3)
    write(os.path.join(s3, "Data", "config.json"),
          json.dumps({"music_folder": s3, "beets_locale": "en"}, indent=2, sort_keys=True) + "\n")
    write(os.path.join(s3, "Data", "beets-library.db"), b"beets")
    write(os.path.join(s3, ".mlo_downloads", ".incomplete", "user", "x.flac"), b"PARTIAL")
    write(os.path.join(s3, ".mlo_trash", "Album 1", "01 - a.flac"), b"AUDIO")
    manifest = {"version": 1, "entries": {"Album 1": {"origin": s3 + "/Artists/Album 1"}}}
    write(os.path.join(s3, ".mlo_trash", ".mlo_manifest.json"),
          json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    migrate()
    s3_cfg = os.path.join(state(s3), "config.json")
    assert os.path.isfile(s3_cfg), f"config.json not in {s3_cfg}"
    assert read_json(s3_cfg) == {"music_folder": s3, "beets_locale": "en"}, read_json(s3_cfg)
    assert os.path.isfile(os.path.join(state(s3), "beets-library.db")), \
        "state file did not move out of <mf>/Data"
    partial = os.path.join(downloads(s3), ".incomplete", "user", "x.flac")
    assert os.path.isfile(partial), f"partial download did not move to {partial}"
    with open(partial, "rb") as f:
        assert f.read() == b"PARTIAL", "partial download arrived altered"
    trashed = os.path.join(bin_dir(s3), "Album 1", "01 - a.flac")
    assert os.path.isfile(trashed), f"trashed album did not move to {trashed}"
    assert read_json(os.path.join(bin_dir(s3), ".mlo_manifest.json")) == manifest, \
        "trash manifest did not move with the bin"
    for old in (os.path.join(s3, "Data"), os.path.join(s3, ".mlo_downloads"),
                os.path.join(s3, ".mlo_trash")):
        assert_empty(old, old)

    # ----------------------------------------------------------------------- #
    # Scenario 4: migration off the INTERMEDIATE layout — <mf>/.mlo_data, the
    # state dir the app used before state moved under <mf>/.mlo. Everything in
    # it lands in <mf>/.mlo/data and the old dir is left empty or gone.
    # ----------------------------------------------------------------------- #
    s4i = make_folder("S4Intermediate")
    write_stub(s4i)
    old_state = legacy_mlo_data(s4i)
    write(os.path.join(old_state, "config.json"),
          json.dumps({"music_folder": s4i, "beets_locale": "ja"},
                     indent=2, sort_keys=True) + "\n")
    sqlite_db(os.path.join(old_state, "playlists.db"), rows=("liked",))
    write(os.path.join(old_state, "beets-config.yaml"), b"intermediate\n")
    write(os.path.join(old_state, "lyrics_cache", "a.json"), b"{}")
    migrate()
    s4i_cfg = os.path.join(state(s4i), "config.json")
    assert os.path.isfile(s4i_cfg), f"config.json not in {s4i_cfg}"
    assert read_json(s4i_cfg) == {"music_folder": s4i, "beets_locale": "ja"}, \
        read_json(s4i_cfg)
    assert db_rows(os.path.join(state(s4i), "playlists.db")) == ["liked"], \
        "playlists.db lost its rows on the way out of .mlo_data"
    with open(os.path.join(state(s4i), "beets-config.yaml"), "rb") as f:
        assert f.read() == b"intermediate\n", "beets config arrived altered"
    assert os.path.isfile(os.path.join(state(s4i), "lyrics_cache", "a.json")), \
        "nested cache did not leave .mlo_data"
    left = files_under(old_state)
    assert not left, f".mlo_data still holds files after the move: {sorted(left)}"

    # ----------------------------------------------------------------------- #
    # Scenario 5: an occupied destination is never clobbered — except by an
    # EMPTY database the app itself created there at startup (that one would
    # otherwise orphan the user's real DB), and the source copy of a refused
    # move is not lost.
    # ----------------------------------------------------------------------- #
    s4 = make_folder("S4Music")
    write_stub(s4)
    sqlite_db(os.path.join(state(s4), "playlists.db"))
    sqlite_db(os.path.join(s4, "Data", "playlists.db"), ["from-the-old-folder"])
    sqlite_db(os.path.join(state(s4), "wishes.db"), ["destination"])
    sqlite_db(os.path.join(s4, "Data", "wishes.db"), ["source"])
    write(os.path.join(state(s4), "beets-config.yaml"), b"destination")
    write(os.path.join(s4, "Data", "beets-config.yaml"), b"source")
    write(os.path.join(state(s4), "lyrics_cache", "kept.json"), b"destination")
    write(os.path.join(s4, "Data", "lyrics_cache", "kept.json"), b"source")
    write(os.path.join(downloads(s4), "kept.flac"), b"destination")
    write(os.path.join(s4, ".mlo_downloads", "kept.flac"), b"source")
    write(os.path.join(bin_dir(s4), "Old", "01.flac"), b"destination")
    write(os.path.join(s4, ".mlo_trash", "Old", "01.flac"), b"source")
    migrate()
    assert db_rows(os.path.join(state(s4), "playlists.db")) == ["from-the-old-folder"], \
        "an empty app-created destination DB blocked the user's real state"
    assert not os.path.exists(os.path.join(s4, "Data", "playlists.db")), \
        "the superseded source DB was left behind"
    assert db_rows(os.path.join(state(s4), "wishes.db")) == ["destination"], \
        "migration overwrote a destination DB that holds rows"
    assert db_rows(os.path.join(s4, "Data", "wishes.db")) == ["source"], \
        "refused move lost the source DB"
    with open(os.path.join(state(s4), "beets-config.yaml"), "rb") as f:
        assert f.read() == b"destination", "migration overwrote a destination file"
    with open(os.path.join(s4, "Data", "beets-config.yaml"), "rb") as f:
        assert f.read() == b"source", "refused move lost the source file"
    with open(os.path.join(state(s4), "lyrics_cache", "kept.json"), "rb") as f:
        assert f.read() == b"destination", "migration overwrote a cached file"
    with open(os.path.join(s4, "Data", "lyrics_cache", "kept.json"), "rb") as f:
        assert f.read() == b"source", "refused move lost the cached file"
    with open(os.path.join(downloads(s4), "kept.flac"), "rb") as f:
        assert f.read() == b"destination", "migration overwrote a downloaded file"
    with open(os.path.join(s4, ".mlo_downloads", "kept.flac"), "rb") as f:
        assert f.read() == b"source", "refused download move lost the source copy"
    with open(os.path.join(bin_dir(s4), "Old", "01.flac"), "rb") as f:
        assert f.read() == b"destination", "migration overwrote a trashed file"
    with open(os.path.join(s4, ".mlo_trash", "Old", "01.flac"), "rb") as f:
        assert f.read() == b"source", "refused trash move lost the source copy"

    # ----------------------------------------------------------------------- #
    # Scenario 6: a second run moves nothing new and breaks nothing.
    # ----------------------------------------------------------------------- #
    s5 = make_folder("S5Music")
    write_stub(s5)
    write(os.path.join(s5, "Data", "config.json"),
          json.dumps({"music_folder": s5}, indent=2, sort_keys=True) + "\n")
    write(os.path.join(s5, "Data", "playlists.db"), b"p")
    write(os.path.join(s5, ".mlo_downloads", ".incomplete", "u", "y.flac"), b"Y")
    write(os.path.join(s5, ".mlo_trash", "A", "01.flac"), b"T")
    write(os.path.join(s5, ".mlo_trash", ".mlo_manifest.json"), b"{\"version\": 1}")
    migrate()
    first = files_under(s5)
    migrate()
    assert files_under(s5) == first, "the second migration changed the tree"
    assert os.path.isfile(os.path.join(downloads(s5), ".incomplete", "u", "y.flac"))
    assert os.path.isfile(os.path.join(bin_dir(s5), "A", "01.flac"))
    assert os.path.isfile(os.path.join(state(s5), "playlists.db"))
    for old in (os.path.join(s5, "Data"), os.path.join(s5, ".mlo_downloads"),
                os.path.join(s5, ".mlo_trash")):
        assert_empty(old, old)

    # ----------------------------------------------------------------------- #
    # Scenario 7: changing the music folder carries the state AND the
    # download/bin dirs to the new folder.
    # ----------------------------------------------------------------------- #
    s6_old, s6_new = make_folder("S6Old"), make_folder("S6New")
    write_stub(s6_old)
    write(os.path.join(s6_old, "Data", "config.json"),
          json.dumps({"music_folder": s6_old, "beets_locale": "ja"},
                     indent=2, sort_keys=True) + "\n")
    write(os.path.join(s6_old, "Data", "wishes.db"), b"wish")
    write(os.path.join(s6_old, ".mlo_downloads", ".incomplete", "u", "z.flac"), b"Z")
    write(os.path.join(s6_old, ".mlo_trash", "A", "01.flac"), b"T")
    write(os.path.join(s6_old, ".mlo_trash", ".mlo_manifest.json"), b"{\"version\": 1}")
    # Fresh process: the startup migration has not run for this folder yet, so
    # the folder change itself is what has to carry everything across.
    cfgmod._MIGRATED = False
    ok = cfgmod.save_config({"music_folder": s6_new, "beets_locale": "de"})
    assert ok, "save_config failed"
    s6_cfg = os.path.join(state(s6_new), "config.json")
    assert os.path.isfile(s6_cfg), f"state did not follow the folder change: {s6_cfg}"
    assert read_json(s6_cfg)["music_folder"] == s6_new, \
        f"save clobbered the new folder: {read_json(s6_cfg)['music_folder']}"
    assert read_json(s6_cfg)["beets_locale"] == "de", \
        f"saved values lost: {read_json(s6_cfg).get('beets_locale')}"
    assert os.path.isfile(os.path.join(state(s6_new), "wishes.db")), \
        "wishes.db did not follow the folder change"
    partial = os.path.join(downloads(s6_new), ".incomplete", "u", "z.flac")
    assert os.path.isfile(partial), f"partial download did not follow to {partial}"
    with open(partial, "rb") as f:
        assert f.read() == b"Z", "moved partial download arrived altered"
    trashed = os.path.join(bin_dir(s6_new), "A", "01.flac")
    assert os.path.isfile(trashed), f"trashed album did not follow to {trashed}"
    assert os.path.isfile(os.path.join(bin_dir(s6_new), ".mlo_manifest.json")), \
        "trash manifest did not follow the folder change"
    assert read_json(stub)["music_folder"] == s6_new, "stub not retargeted"
    for old in (os.path.join(s6_old, "Data"), os.path.join(s6_old, ".mlo_downloads"),
                os.path.join(s6_old, ".mlo_trash"), downloads(s6_old), bin_dir(s6_old)):
        assert_empty(old, old)

    # ----------------------------------------------------------------------- #
    # Scenario 8: importing the app must not create state files (an empty
    # playlists.db planted at import time used to make the no-clobber move
    # skip the user's real database), and a database that IS already empty
    # at the destination must still give way to the real one.
    # ----------------------------------------------------------------------- #
    import importlib

    s7 = make_folder("S7Music")
    write_stub(s7)
    os.environ["MLO_MUSIC_FOLDER"] = s7
    for name in ("server.playlists", "server.wishes"):
        sys.modules.pop(name, None)
        importlib.import_module(name)
    for name in ("playlists.db", "wishes.db"):
        assert not os.path.exists(os.path.join(state(s7), name)), \
            f"importing the app created {name} before the migration ran"
    # a destination DB that already exists but holds nothing must not win
    sqlite_db(os.path.join(state(s7), "playlists.db"))
    sqlite_db(os.path.join(s7, "Data", "playlists.db"), rows=("real-1", "real-2"))
    migrate()
    assert db_rows(os.path.join(state(s7), "playlists.db")) == ["real-1", "real-2"], \
        "an empty database at the destination blocked the user's real one"
    os.environ.pop("MLO_MUSIC_FOLDER", None)

    # ----------------------------------------------------------------------- #
    # Scenario 9: a scope pointing at a DIFFERENT install (MLO_MUSIC_FOLDER,
    # a probe run) must never be able to strand that install's state — the
    # carry copies, the source keeps every file. Same-folder legacy dirs are
    # still MOVED (they are this install's old layout, not another one's).
    # Both previous-folder layouts are sources: the intermediate .mlo_data and
    # the current .mlo/data.
    # ----------------------------------------------------------------------- #
    s8_other = make_folder("S8OtherInstall")       # stands in for the real one
    s8_scope = make_folder("S8Scope")              # a throwaway scope
    write_stub(s8_other)
    write(os.path.join(state(s8_other), "config.json"),
          json.dumps({"music_folder": s8_other, "keep": True}, indent=2, sort_keys=True) + "\n")
    sqlite_db(os.path.join(state(s8_other), "playlists.db"), rows=("liked",))
    sqlite_db(os.path.join(legacy_mlo_data(s8_other), "wishes.db"), rows=("wished",))
    os.environ["MLO_MUSIC_FOLDER"] = s8_scope      # ...but run against the scope
    migrate()
    carried = os.path.join(state(s8_scope), "config.json")
    assert os.path.isfile(carried), "other install's config did not carry into the scope"
    assert read_json(carried)["music_folder"] == s8_scope, read_json(carried)
    assert read_json(carried)["keep"] is True, "the carried config lost fields"
    assert db_rows(os.path.join(state(s8_scope), "playlists.db")) == ["liked"], \
        "the other install's .mlo/data did not carry into the scope"
    assert db_rows(os.path.join(state(s8_scope), "wishes.db")) == ["wished"], \
        "the other install's .mlo_data did not carry into the scope"
    assert os.path.isfile(os.path.join(state(s8_other), "config.json")), \
        "the other install lost its config to the scope"
    assert db_rows(os.path.join(state(s8_other), "playlists.db")) == ["liked"], \
        "the other install lost its playlists to the scope"
    assert db_rows(os.path.join(legacy_mlo_data(s8_other), "wishes.db")) == ["wished"], \
        "the other install's intermediate state was moved, not copied"
    # a legacy dir of the scope's OWN folder is still moved, not copied
    s8_music = make_folder("S8Music")
    write(os.path.join(s8_music, "Data", "legacy.db"), b"own")
    os.environ["MLO_MUSIC_FOLDER"] = s8_music
    migrate()
    assert os.path.isfile(os.path.join(state(s8_music), "legacy.db")), \
        "the folder's own legacy state did not move"
    assert not os.path.exists(os.path.join(s8_music, "Data", "legacy.db")), \
        "the folder's own legacy state was copied instead of moved"
    os.environ.pop("MLO_MUSIC_FOLDER", None)

    print("PASS  all config-migration scenarios")
finally:
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    if OUT:
        os.environ["MLO_MUSIC_FOLDER"] = OUT
    for mod, path in real_config_files.items():
        mod.CONFIG_FILE = path
    for mod, path in real_legacy.items():
        if path is not None:
            mod.LEGACY_DATA_DIR = path
    cfgmod._MIGRATED = False
    shutil.rmtree(tmp, ignore_errors=True)
