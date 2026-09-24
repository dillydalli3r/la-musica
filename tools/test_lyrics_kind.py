#!/usr/bin/env python3
"""The lyrics KIND in the library payload and the wizard's scan — pinned.

A surface can only say whether a track HAS lyrics; the owner asked every one of
them to say WHICH KIND they are (synced/timed or plain) and to show a plain
lyric as the failing state while the user's own ``lyrics_allow_plain`` says
plain is not acceptable. The fact behind that is one payload field:

    lyrics_kind: "synced" | "plain" | null

derived by mlo.lyrics.stored_lyrics_kind from the two stored texts the grader
already reads (the LYRICS/UNSYNCEDLYRICS tag and the ``.lrc`` sidecar) and
stamped by mlo.grader beside ``lyrics_embedded`` / ``lyrics_lrc``.

Pinned here, on a temp library with one track of each shape:

  * a timed ``.lrc``               → "synced"
  * embedded lyrics with timestamps → "synced"
  * embedded lyrics without         → "plain"
  * a timed ``.lrc`` PLUS plain embedded lyrics → "synced" (both sources at
    once IS synced — the player follows the timed one)
  * no lyrics at all                → null

plus the consistency the field is built on (``lyrics_present`` is exactly
``lyrics_kind is not null``, and the two boolean flags agree with it), the same
field on ``GET /api/album/scan-tracks`` (the import wizard's own read of the
same tracks), and the query catalogue entry a Library column sorts/filters by.

Run: python tools/test_lyrics_kind.py
Exit 0 = pass.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.audio import AudioFile  # noqa: E402
from server import api_query, library as mlo_library  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import)
from fastapi.testclient import TestClient  # noqa: E402

TMP = tempfile.mkdtemp(prefix="mlo-lyrkind-")
MUSIC = os.path.join(TMP, "music")
ALBUM = os.path.join(MUSIC, "Artists", "Kind Artist", "2020 - Kind Album")
os.makedirs(ALBUM)
os.makedirs(os.path.join(MUSIC, ".mlo", "data"))

CFG = {"music_folder": MUSIC}

# A real (if silent) MP3 frame: the album's identity is read from the tracks'
# own tags, and a junk byte string would be an unreadable file.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413

TIMED_LRC = "[00:01.00]first line\n[00:05.00]second line\n"
PLAIN_EMBEDDED = "first line\nsecond line\n"


def write_track(name, tag_lyrics=None, sidecar=None):
    path = os.path.join(ALBUM, name)
    with open(path, "wb") as f:
        f.write(_MP3_FRAME * 40)
    af = AudioFile(path)
    for key, value in {"ALBUMARTIST": "Kind Artist", "ALBUM": "Kind Album",
                       "TITLE": name}.items():
        assert af.set_tag(key, value), (path, key)
    if tag_lyrics is not None:
        assert af.set_lyrics(tag_lyrics), path
    if sidecar is not None:
        with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as fh:
            fh.write(sidecar)
    return path


write_track("1-01 TimedLrc.mp3", sidecar=TIMED_LRC)
write_track("1-02 EmbeddedTimed.mp3", tag_lyrics=TIMED_LRC)
write_track("1-03 EmbeddedPlain.mp3", tag_lyrics=PLAIN_EMBEDDED)
write_track("1-04 Both.mp3", tag_lyrics=PLAIN_EMBEDDED, sidecar=TIMED_LRC)
write_track("1-05 None.mp3")

EXPECTED = {
    "1-01 TimedLrc.mp3": "synced",
    "1-02 EmbeddedTimed.mp3": "synced",
    "1-03 EmbeddedPlain.mp3": "plain",
    "1-04 Both.mp3": "synced",
    "1-05 None.mp3": None,
}

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"{name} — {detail}")
        print(f"FAIL {name} — {detail}")


# --------------------------------------------------------------------------- #
# 1) the library payload (`server.library.build_album` — what /api/library and
#    the album page serve)
# --------------------------------------------------------------------------- #
album = mlo_library.build_album(ALBUM, CFG)
check("build_album returns an album", bool(album) and bool(album.get("tracks")),
      repr(album)[:200])
rows = {tr["file"]: tr for tr in (album or {}).get("tracks", [])}
check("every fixture track is in the payload", set(rows) == set(EXPECTED),
      f"{sorted(rows)} != {sorted(EXPECTED)}")

for name, want in EXPECTED.items():
    tr = rows.get(name, {})
    check(f"{name}: lyrics_kind", tr.get("lyrics_kind") == want,
          f"{tr.get('lyrics_kind')!r} != {want!r}")
    # One fact, two fields: presence IS the kind not being null, and the two
    # flags the payload has always carried agree with it — a reader of either
    # cannot see a different story (the timed-.lrc-plus-plain-embedded track
    # has BOTH flags set and is still, correctly, one kind: synced).
    check(f"{name}: lyrics_present follows the kind",
          tr.get("lyrics_present") == (want is not None),
          repr(tr.get("lyrics_present")))
    check(f"{name}: presence matches the payload's own flags",
          bool(tr.get("lyrics_present")) == bool(tr.get("lyrics_embedded")
                                                 or tr.get("lyrics_lrc")),
          f"{tr.get('lyrics_embedded')!r}/{tr.get('lyrics_lrc')!r}")

both = rows.get("1-04 Both.mp3", {})
check("both sources at once: the timed .lrc is credited",
      both.get("lyrics_lrc") is True and both.get("lyrics_embedded") is True
      and both.get("lyrics_kind") == "synced", repr(both)[:200])

# An album with no lyrics at all still carries the field, on every track: no
# reader has to treat "absent" as a case of its own.
check("the field is present (not missing) on every track row",
      all("lyrics_kind" in tr for tr in rows.values()), sorted(rows))

# --------------------------------------------------------------------------- #
# 2) the same field on the import wizard's read of the same folder
#    (GET /api/album/scan-tracks), which the wizard's lyrics step runs on
# --------------------------------------------------------------------------- #
mlo_main.load_config = lambda *a, **k: dict(CFG)
client = TestClient(mlo_main.app)   # no lifespan: no workers, no slskd boot
r = client.get("/api/album/scan-tracks", params={"path": ALBUM})
check("scan-tracks answers", r.status_code == 200, r.text[:200])
scanned = {t["file"]: t for t in (r.json().get("tracks") if r.status_code == 200 else [])}
check("scan-tracks lists the fixture tracks", set(scanned) == set(EXPECTED),
      f"{sorted(scanned)}")
for name, want in EXPECTED.items():
    t = scanned.get(name, {})
    check(f"scan-tracks {name}: lyrics_kind", t.get("lyrics_kind") == want,
          f"{t.get('lyrics_kind')!r} != {want!r}")
    check(f"scan-tracks {name}: lyrics_present follows the kind",
          t.get("lyrics_present") == (want is not None), repr(t.get("lyrics_present")))

# --------------------------------------------------------------------------- #
# 3) the query field the Library's own column sorts/filters by
# --------------------------------------------------------------------------- #
catalogue = {f["field"]: f for g in api_query.catalogue()["groups"] for f in g["fields"]}
entry = catalogue.get("library.lyrics_kind")
check("library.lyrics_kind is in the query catalogue", bool(entry), "missing")
if entry:
    check("…with the two kind values", entry.get("values") == ["synced", "plain"],
          repr(entry.get("values")))
    check("…tracks-only", entry.get("targets") == ["tracks"], repr(entry.get("targets")))

shutil.rmtree(TMP, ignore_errors=True)

if failures:
    print(f"\nlyrics kind: {len(failures)} check(s) FAILED")
    raise SystemExit(1)
print("lyrics kind: the payload field, the flags it folds and the wizard's "
      "scan — all assertions passed")
