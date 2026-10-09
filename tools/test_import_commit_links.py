"""The wizard's Links step writes every link in ONE pass per track.

"Saving links…" used to cost TWO full passes over the album: `/api/import/commit`
stamped the MusicBrainz release id and the RateYourMusic album page, and a second
call (`/api/mb/assign`) rewrote every file AGAIN for `RATEYOURMUSIC_ARTIST`. Both
are whole-container rewrites (mutagen copies the file, `mlo.atomic.rewrite_via`
swaps it in), so a 12-track album paid ~24 rewrites for three tags that fit in
one — that was the step's whole duration.

The route now carries all three links, so each track is rewritten ONCE, and a
re-run (every value already stored) rewrites NOTHING. Both are counted here at
the single point every in-place rewrite goes through, which is the causal
measurement: the wall time is the disk's, the rewrite count is ours.

Run:  python tools/test_import_commit_links.py
"""
import os
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in sorted(os.listdir(_deps)):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break
assert FLAC_EXE, "flac.exe not found under .dependencies"

from mlo import atomic                                  # noqa: E402
from server import main as app                          # noqa: E402
from server.main import ImportCommit                    # noqa: E402

TMP = tempfile.mkdtemp(prefix="mlo_import_commit_links_")
MF = os.path.join(TMP, "music")
ALBUM = os.path.join(MF, "Artists", "Test Artist", "2020 - Test Album")
os.makedirs(ALBUM)
TRACKS = 3
MB_LINK = "https://musicbrainz.org/release/11111111-1111-1111-1111-111111111111"
RYM_ALBUM = "https://rateyourmusic.com/release/album/test-artist/test-album/"
RYM_ARTIST = "https://rateyourmusic.com/artist/test-artist"
RYM_SONG = "https://rateyourmusic.com/release/album/test-artist/test-album/other/"  # not an artist page

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok {label}")


def make_flac(path):
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


for i in range(1, TRACKS + 1):
    make_flac(os.path.join(ALBUM, f"{i:02d} - Track.flac"))


def tags_of(path):
    from mutagen.flac import FLAC
    f = FLAC(path)
    return {k: (f.get(k) or [""])[0] for k in
            ("MUSICBRAINZ_ALBUMID", "RATEYOURMUSIC_ALBUM", "RATEYOURMUSIC_ARTIST")}


# --------------------------------------------------------------------------- #
# Count the rewrites at their single choke point
# --------------------------------------------------------------------------- #
_real_rewrite = atomic.rewrite_via
rewrites = []


def counting(dest, write_fn):
    rewrites.append(os.path.normpath(str(dest)))
    return _real_rewrite(dest, write_fn)


atomic.rewrite_via = counting

cfg = {"music_folder": MF}
app.load_config = lambda: dict(cfg)

print("== one pass: the commit carries the artist link too ==")
rewrites.clear()
res = app.import_commit(ImportCommit(target_dir=ALBUM, mb_link=MB_LINK,
                                     rym_link=RYM_ALBUM,
                                     rym_artist_link=RYM_ARTIST, staged=True))
ok(res["ok"] is True, "the route answered ok")
ok(len(rewrites) == TRACKS,
   f"one rewrite per track, not two ({len(rewrites)} for {TRACKS} tracks)")

for i in range(1, TRACKS + 1):
    tags = tags_of(os.path.join(ALBUM, f"{i:02d} - Track.flac"))
    ok(tags["MUSICBRAINZ_ALBUMID"] == "11111111-1111-1111-1111-111111111111",
       f"track {i}: the MusicBrainz release id landed")
    ok(tags["RATEYOURMUSIC_ALBUM"] == RYM_ALBUM,
       f"track {i}: the album page landed")
    ok(tags["RATEYOURMUSIC_ARTIST"] == RYM_ARTIST,
       f"track {i}: the artist page landed in the SAME write")

print("== a re-run writes nothing at all ==")
rewrites.clear()
res = app.import_commit(ImportCommit(target_dir=ALBUM, mb_link=MB_LINK,
                                     rym_link=RYM_ALBUM,
                                     rym_artist_link=RYM_ARTIST, staged=True))
ok(res["ok"] is True, "the second run answered ok")
ok(rewrites == [], f"not one track was rewritten ({len(rewrites)})")
ok(all(tags_of(os.path.join(ALBUM, f"{i:02d} - Track.flac"))["RATEYOURMUSIC_ARTIST"]
       == RYM_ARTIST for i in range(1, TRACKS + 1)),
   "and the tags are still the ones that were written")

print("== only an ARTIST page is stored as the artist link ==")
# A song/other URL in the artist field is a wrong link forever; the route checks
# the page's own kind (the wizard's field already did) and writes nothing.
album_dir2 = os.path.join(MF, "Artists", "Test Artist", "2020 - Other Album")
os.makedirs(album_dir2)
make_flac(os.path.join(album_dir2, "01 - Track.flac"))
rewrites.clear()
app.import_commit(ImportCommit(target_dir=album_dir2, mb_link=MB_LINK,
                               rym_artist_link=RYM_SONG, staged=True))
tags = tags_of(os.path.join(album_dir2, "01 - Track.flac"))
ok(tags["RATEYOURMUSIC_ARTIST"] == "", "a non-artist URL is refused")
ok(tags["MUSICBRAINZ_ALBUMID"] != "", "while the release id still lands")

atomic.rewrite_via = _real_rewrite
print(f"import commit links: all {passed} assertions passed")
