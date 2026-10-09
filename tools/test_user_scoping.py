#!/usr/bin/env python3
"""Two users must not see each other's playlists, likes or favourites.

This is the contract the per-user work exists for, and the way it fails is
SILENT: one read or write that forgets its `user=` argument still returns a
perfectly good-looking list — someone else's. No error, no empty page, just two
people's libraries quietly mixed. So every public function in
`server.playlists` is driven here for two named scopes and for the default
scope, and the assertions are about what a *different* user can see.

The default scope ("" — an unclaimed install, and every row written before
users existed) is checked to still work: it is the scope the migration leaves
untouched, and breaking it would orphan the data of every existing install.

Run:  python tools/test_user_scoping.py   (exit 0 = pass, 1 = failure)
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.paths import trash_dir              # noqa: E402
from server import playlists as pl           # noqa: E402

fails = []


def check(ok, label, extra=""):
    if not ok:
        fails.append(f"{label}{(': ' + str(extra)) if extra else ''}")


tmp = tempfile.mkdtemp(prefix="mlo_userscope_")
pl.db_path = lambda: os.path.join(tmp, "playlists.db")

ALICE, BOB = "alice", "bob"
A_TRACK, B_TRACK, SHARED = "C:/music/a.flac", "C:/music/b.flac", "C:/music/same.flac"

# ── playlists ───────────────────────────────────────────────────────────────
a_pl = pl.create_playlist("Alice list", user=ALICE)
b_pl = pl.create_playlist("Bob list", user=BOB)
d_pl = pl.create_playlist("Default list")

check([p["name"] for p in pl.list_playlists(user=ALICE)] == ["Alice list"],
      "alice sees only her list", pl.list_playlists(user=ALICE))
check([p["name"] for p in pl.list_playlists(user=BOB)] == ["Bob list"],
      "bob sees only his list")
check([p["name"] for p in pl.list_playlists()] == ["Default list"],
      "the default scope keeps its own")

pl.add_tracks(a_pl, [A_TRACK], user=ALICE)
pl.add_tracks(b_pl, [B_TRACK], user=BOB)
pl.add_tracks(d_pl, [SHARED])

check(pl.get_playlist(a_pl, user=ALICE)["tracks"] == [A_TRACK],
      "the owner reads their own tracks", pl.get_playlist(a_pl, user=ALICE)["tracks"])
check(pl.get_playlist(a_pl, user=BOB) is None,
      "a different user cannot read the playlist at all")
check(pl.export_m3u8(a_pl, user=BOB) is None,
      "and cannot export it")
check(pl.add_tracks(a_pl, ["C:/music/evil.flac"], user=BOB) == 0,
      "and cannot add to it")
check(pl.delete_playlist(a_pl, user=BOB) is False,
      "and cannot delete it")

# ── likes: the same track liked by two people is two rows ───────────────────
check(pl.toggle_like(A_TRACK, user=ALICE) is True, "alice likes a track")
check(pl.toggle_like(SHARED, user=ALICE) is True, "alice likes a second")
check(pl.toggle_like(SHARED, user=BOB) is True,
      "bob can like the SAME track — it is his row, not alice's")
check(pl.toggle_like(B_TRACK) is True, "the default scope likes its own")

check(sorted(pl.list_likes(user=ALICE)) == sorted([A_TRACK, SHARED]),
      "alice's likes are hers", pl.list_likes(user=ALICE))
check(pl.list_likes(user=BOB) == [SHARED],
      "bob's likes are his", pl.list_likes(user=BOB))
check(pl.is_liked(A_TRACK, user=BOB) is False,
      "bob has not liked what alice liked")
check(pl.is_liked(SHARED, user=BOB) is True, "and has liked what he liked")
check(pl.toggle_like(SHARED, user=ALICE) is False,
      "unliking removes only the caller's row")
check(pl.is_liked(SHARED, user=BOB) is True,
      "…and bob's row survives it")

# ── favourites ──────────────────────────────────────────────────────────────
pl.toggle_favorite("album", "C:/music/Album A", user=ALICE)
pl.toggle_favorite("artist", "C:/music/Artist B", user=BOB)

a_fav = pl.list_favorites(user=ALICE)
b_fav = pl.list_favorites(user=BOB)
check(a_fav["albums"] == ["C:/music/Album A"] and a_fav["artists"] == [],
      "alice's favourites are hers", a_fav)
check(b_fav["artists"] == ["C:/music/Artist B"] and b_fav["albums"] == [],
      "bob's favourites are his", b_fav)

# ── a row written before users existed ──────────────────────────────────────
# The migration adds the `user` column and moves nothing, so legacy rows land
# in the default scope and must stay readable there.
with pl._conn() as c:
    c.execute("INSERT INTO likes (path, liked_at) VALUES (?, ?)",
              ("C:/music/legacy.flac", 1.0))
check("C:/music/legacy.flac" in pl.list_likes(),
      "a pre-users like is readable in the default scope")
check("C:/music/legacy.flac" not in pl.list_likes(user=ALICE),
      "and is not attributed to a named user")

# ── the trash bin follows the user, downloads do not ────────────────────────
check(trash_dir("C:/music") .replace("\\", "/").endswith("/.mlo/trash/default"),
      "the default scope's bin is the `default` segment",
      trash_dir("C:/music"))
check(trash_dir("C:/music", ALICE).replace("\\", "/").endswith("/.mlo/trash/alice"),
      "a named user's bin is theirs", trash_dir("C:/music", ALICE))
check(trash_dir("C:/music", ALICE) != trash_dir("C:/music", BOB),
      "two users do not share a bin")

# A username reaches this from a session, and a session's name can come from
# the PUBLIC setup route — so a name that is not one plain segment must never
# become a parent path. `trash_dir(mf, "..")` used to be `<mf>/.mlo`, whose own
# "delete" would then have removed the app's data directory.
for evil in ("..", ".", "a/b", "a\\b", "C:x", "", "   "):
    got = trash_dir("C:/music", evil).replace("\\", "/")
    check(got.endswith("/.mlo/trash/default"),
          f"username {evil!r} cannot escape the bin", got)
check(trash_dir("C:/music", "alice") .replace("\\", "/").endswith("/.mlo/trash/alice"),
      "a plain name still gets its own bin")

# ── a username cannot escape its own folder ────────────────────────────────
from server import auth as auth_mod          # noqa: E402
for bad in ("", "   ", "a/b", "..", "a\\b", "line\nbreak", "x" * 65):
    check(auth_mod.user_problem(bad) != "", f"username {bad!r} is refused")
for good in ("alice", "Bob", "a b", "user.name", "ünicode"):
    check(auth_mod.user_problem(good) == "",
          f"username {good!r} is allowed", auth_mod.user_problem(good))

shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("user scoping: FAILED")
    for line in fails:
        print("  - " + line)
    sys.exit(1)
print("user scoping: all assertions passed")
sys.exit(0)
