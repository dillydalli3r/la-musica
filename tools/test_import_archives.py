#!/usr/bin/env python3
"""Archives in the import path (mlo.archives + the /api/import routes).

A dropped or picked archive is untrusted input, and this is the contract the
import path holds it to:

  * a member that would write OUTSIDE the staging folder — an absolute path
    (POSIX "/…", a Windows drive, a UNC share) or a ".." segment — is refused
    BEFORE anything is written, with the member named, and the destination is
    left exactly as it was;
  * a symbolic link, a hard link and a device node are refused the same way
    (mlo.archives is the one place that rule lives, for every format);
  * a format with no available extractor says WHICH format needs what instead
    of unpacking half the archive — `.rar`/`.7z` without 7-Zip included;
  * a nested archive is a file in the tree, never a second extraction of bytes
    the user did not hand over;
  * a rip inside an archive imports as the folder it is: the tracks AND their
    .cue/.log land in the album through `POST /api/import/unpack` followed by
    the SAME `POST /api/import/upload` a folder upload uses, with the same
    partial marking the sheets produce;
  * an archive that holds no audio reports `audio: 0` rather than making an
    empty album;
  * the server-side `staged` list the upload route accepts may only name files
    inside a folder THIS app unpacked — every other path is refused, which is
    what keeps the route from placing any file on the server into the library.

Nothing here touches the network and no external tool is required: the
fixtures are crafted archives (zip and tar by hand, `.7z` only when this host
has 7-Zip) and the rip is WAV + a cue sheet. The music folder is a temp
directory with both stores redirected into it, and the routes are driven
through the app's own HTTP surface (server.main.app) exactly as the wizard
drives them.

Run:  python tools/test_import_archives.py   (exit 0 pass, 1 fail)
"""
import atexit
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import wave
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app's paths resolve through the music folder the moment they
# are first touched, so the scope is redirected BEFORE server.main is imported.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-arcreg-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-arcreg-lib-")
WORK = tempfile.mkdtemp(prefix="mlo-arcreg-work-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF}, f)
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF, WORK)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
for _t in (MF, WORK):
    if REAL:
        assert not _t.replace("\\", "/").lower().startswith(REAL.lower()), \
            f"temp fixture {_t} sits inside the real music folder {REAL}"

FAILS = []


def check(cond, label, extra=""):
    got = f" — {extra}" if (extra and not cond) else ""
    print(("  PASS  " if cond else "  FAIL  ") + label + got)
    if not cond:
        FAILS.append(label)


def eq(got, want, label, extra=""):
    check(got == want, label, extra or f"got {got!r}, want {want!r}")


from fastapi.testclient import TestClient  # noqa: E402

from mlo import archives  # noqa: E402
from mlo.paths import library_root, mlo_root  # noqa: E402
from server import main as mlo_main  # noqa: E402

CLIENT = TestClient(mlo_main.app)
SEVENZ = archives.find_7z()


class NoSevenZip:
    """This host without 7-Zip: the only question a `.rar` can be asked."""

    def __enter__(self):
        self.real = archives.find_7z
        archives.find_7z = lambda: None
        return self

    def __exit__(self, *exc):
        archives.find_7z = self.real
        return False


def zip_bytes(entries, base=None):
    """A zip whose member names are EXACTLY what is given (the whole point:
    zipfile writes whatever name it is handed, which is how a crafted archive
    is built). Values may be bytes or a path to read."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, value in entries:
            data = open(os.path.join(base, value), "rb").read() if base else value
            zf.writestr(name, data)
    return buf.getvalue()


def zip_with_special(path, name, kind):
    """A zip holding ONE entry that is not a plain file: `kind` is the Unix
    mode to claim for it (a symbolic link, a character device)."""
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(name)
        info.external_attr = (kind | 0o777) << 16
        zf.writestr(info, b"../target" if kind == stat.S_IFLNK else b"")
        zf.writestr("ok.txt", b"fine")


def tar_with(entries, path):
    """A tar whose members are built as asked — type included, because
    tarfile will happily write a character device or a symlink entry."""
    with tarfile.open(path, "w") as tf:
        for name, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = 1
                tf.addfile(info, io.BytesIO(b"x"))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tf.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = "elsewhere"
                tf.addfile(info)
            elif kind == "chardev":
                info.type = tarfile.CHRTYPE
                info.devmajor, info.devminor = 1, 3
                tf.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tf.addfile(info)


def _pathish(s):
    """One spelling of a path in TEXT: a repr'd name ("C:\\\\Windows\\\\evil")
    and a plain one ("C:/Windows/evil") both read the same, so an assertion
    about *which* member a message names does not depend on the platform's
    separator or on how repr escaped it."""
    return str(s).replace("\\\\", "/").replace("\\", "/")


def extract_into(path, label, dest=None):
    """Run the extractor the way the route does; answer (dest, error, kept)."""
    dest = dest or tempfile.mkdtemp(prefix="extract-", dir=WORK)
    before = sorted(os.listdir(WORK))
    error = None
    try:
        archives.extract(path, dest, log=lambda _m: None)
    except archives.ArchiveError as e:
        error = e
    outside = sorted(set(os.listdir(WORK)) - set(before) - {os.path.basename(dest)})
    return dest, error, outside


# --------------------------------------------------------------------------- #
# 1. a crafted member never becomes a file
# --------------------------------------------------------------------------- #
print("== crafted archives are refused before anything is written ==")

BAD_NAMES = [
    ("a zip member that escapes upward", "../escape.wav"),
    ("a zip member with a POSIX absolute path", "/etc/passwd"),
    ("a zip member naming a Windows drive", "C:\\Windows\\evil.txt"),
    ("a zip member naming a UNC share", "\\\\server\\share\\evil.txt"),
    ("a zip member that escapes from a subfolder", "album/../../escape.wav"),
]
for label, name in BAD_NAMES:
    crafted = os.path.join(WORK, "crafted.zip")
    with open(crafted, "wb") as fh:
        fh.write(zip_bytes([(name, b"nope")]))
    dest, error, outside = extract_into(crafted, label)
    check(isinstance(error, archives.UnsafeArchiveError), label, f"got {error!r}")
    # The refusal names the member the way it was WRITTEN (a repr, so a
    # backslash arrives doubled). Normalise both sides to one path spelling:
    # the assertion is "the refusal names the member", not how repr renders it.
    check(bool(error) and _pathish(name) in _pathish(str(error)),
          f"{label} — the refusal names the member", str(error))
    eq(os.listdir(dest), [], f"{label} — nothing was extracted")
    eq(outside, [], f"{label} — nothing was written beside the staging folder")

TAR_CASES = [
    ("a tar member that escapes upward", ("../escape.wav", "file")),
    ("a tar member with an absolute path", ("/etc/passwd", "file")),
    ("a tar symbolic link", ("link", "symlink")),
    ("a tar hard link", ("hard", "hardlink")),
    ("a tar character device", ("null", "chardev")),
    ("a tar fifo", ("pipe", "fifo")),
]
for label, entry in TAR_CASES:
    crafted = os.path.join(WORK, "crafted.tar")
    tar_with([entry, ("ok.txt", "file")], crafted)
    dest, error, outside = extract_into(crafted, label)
    check(isinstance(error, archives.UnsafeArchiveError), label, f"got {error!r}")
    eq(os.listdir(dest), [], f"{label} — nothing was extracted")
    eq(outside, [], f"{label} — nothing was written beside the staging folder")

for label, member, kind in (("a zip symbolic link", "link", stat.S_IFLNK),
                            ("a zip device node", "dev", stat.S_IFCHR),
                            ("a zip fifo", "pipe", stat.S_IFIFO)):
    special = os.path.join(WORK, f"{member}.zip")
    zip_with_special(special, member, kind)
    dest, error, outside = extract_into(special, label)
    check(isinstance(error, archives.UnsafeArchiveError), label, f"got {error!r}")
    check(bool(error) and member in str(error), f"{label} — the refusal names the member", str(error))
    eq(os.listdir(dest), [], f"{label} — nothing was extracted")
    eq(outside, [], f"{label} — nothing was written beside the staging folder")

if SEVENZ:
    # 7-Zip can be asked to store an ABSOLUTE member ("-spf" keeps the full
    # path), which is the one crafted shape the standard library cannot write
    # in a .7z: the listing it produces is what the refusal reads.
    plain = os.path.join(WORK, "abs-src.txt")
    with open(plain, "wb") as fh:
        fh.write(b"x")
    abs7z = os.path.join(WORK, "abs.7z")
    # "-spf" stores the member's FULL path, which is the absolute member this
    # host's own 7-Zip is able to write (Python cannot craft one in a .7z).
    subprocess.run([SEVENZ, "a", "-t7z", "-spf", abs7z, plain],
                   capture_output=True, check=False)
    if os.path.isfile(abs7z):
        dest, error, outside = extract_into(abs7z, "a .7z member with an absolute path")
        check(isinstance(error, archives.UnsafeArchiveError),
              "a .7z member with an absolute path is refused", f"got {error!r}")
        eq(os.listdir(dest), [], "…and nothing was extracted")
else:
    print("  (no 7-Zip on this host — the .7z case is skipped)")

# --------------------------------------------------------------------------- #
# 2. what the format costs: an honest answer, never half an archive
# --------------------------------------------------------------------------- #
print("\n== a format with no extractor says so ==")

rar = os.path.join(WORK, "album.rar")
with open(rar, "wb") as fh:
    fh.write(b"Rar!\x1a\x07\x00 nothing readable here")
with NoSevenZip():
    dest, error, outside = extract_into(rar, "rar")
check(isinstance(error, archives.NoExtractorError), "a .rar without 7-Zip is refused", f"got {error!r}")
check(bool(error) and "7-Zip" in str(error), "…and the refusal names what would read it", str(error))
eq(os.listdir(dest), [], "…and nothing was extracted")

gz = os.path.join(WORK, "album.gz")
with open(gz, "wb") as fh:
    fh.write(b"\x1f\x8b\x08\x00nope")
dest, error, _outside = extract_into(gz, "gz")
check(isinstance(error, archives.NoExtractorError), "a .gz nothing here reads is refused", f"got {error!r}")
check(bool(error) and ".zip" in str(error), "…and the refusal lists what IS supported", str(error))

nested = os.path.join(WORK, "outer.zip")
with open(nested, "wb") as fh:
    fh.write(zip_bytes([("album/01 - One.wav", b"RIFF"), ("album/inner.zip", b"PK\x03\x04late")]))
dest, error, _outside = extract_into(nested, "nested")
eq(error, None, "a zip inside a zip does not stop the outer archive")
eq(sorted(os.listdir(dest)), ["album"], "…the outer archive unpacks")
eq(sorted(os.listdir(os.path.join(dest, "album"))),
   ["01 - One.wav", "inner.zip"],
   "…and the inner archive is left as a file, never unpacked again")

# --------------------------------------------------------------------------- #
# 3. the routes: a rip in an archive is the folder it contains
# --------------------------------------------------------------------------- #
print("\n== a rip in a zip imports as the folder does ==")

RIP = os.path.join(WORK, "rip")
os.makedirs(os.path.join(RIP, "The Album"), exist_ok=True)
ALBUM_SRC = os.path.join(RIP, "The Album")
TRACKS = ["01 - One.wav", "02 - Two.wav"]
CUE_NAMES = ["01 - One.wav", "02 - Two.wav", "03 - Three.wav", "04 - Four.wav"]


def make_wav(path, seconds):
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * int(44100 * seconds))


for name, secs in zip(TRACKS, (3.0, 5.0)):
    make_wav(os.path.join(ALBUM_SRC, name), secs)
# A four-track rip whose sheets are in the archive and whose last two tracks
# are NOT: what comes out of the archive must be marked partial exactly as the
# folder upload is.
with open(os.path.join(ALBUM_SRC, "rip.cue"), "w", encoding="utf-8") as fh:
    fh.write('PERFORMER "The Artist"\nTITLE "The Album"\n')
    for i, name in enumerate(CUE_NAMES, 1):
        fh.write(f'FILE "{name}" WAVE\n  TRACK {i:02d} AUDIO\n'
                 f'    TITLE "Track {i}"\n    PERFORMER "The Artist"\n')
with open(os.path.join(ALBUM_SRC, "rip.log"), "w", encoding="utf-8") as fh:
    fh.write("Exact Audio Copy V1.6\n\nTOC of the extracted CD\n"
             "     Track |   Start  |  Length  | Start sector | End sector\n"
             "    ---------------------------------------------------------\n"
             "        1  |  0:00.00 |  0:03.00 |         0    |     2249\n"
             "        2  |  0:03.00 |  0:05.00 |      2250    |     5999\n"
             "        3  |  0:08.00 |  0:04.00 |      6000    |     8999\n"
             "        4  |  0:12.00 |  0:06.00 |      9000    |    13499\n")

# The rip itself, zipped as its CONTENTS (the common shape: the album folder's
# files at the archive root).
rip_zip = os.path.join(WORK, "rip.zip")
with zipfile.ZipFile(rip_zip, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in os.listdir(ALBUM_SRC):
        zf.write(os.path.join(ALBUM_SRC, name), name)

# (a) the folder, through the ordinary multipart upload.
files = []
for name in sorted(os.listdir(ALBUM_SRC)):
    with open(os.path.join(ALBUM_SRC, name), "rb") as fh:
        files.append(("files", (name, fh.read())))
res = CLIENT.post("/api/import/upload?target_dir=Folder%20Rip", files=files)
eq(res.status_code, 200, "the folder upload is accepted", res.text[:300])
folder_album = os.path.join(library_root(MF), "Folder Rip")

# (b) the archive, through unpack + the SAME upload route with `staged`.
with open(rip_zip, "rb") as fh:
    res = CLIENT.post("/api/import/unpack", files={"file": ("rip.zip", fh.read())})
eq(res.status_code, 200, "the archive is unpacked", res.text[:300])
tree = res.json()
eq(tree["label"], "rip.zip", "the reply names the archive")
eq(tree["audio"], 2, "the reply counts the audio it found")
eq(sorted(f["relPath"] for f in tree["files"]),
   ["01 - One.wav", "02 - Two.wav", "rip.cue", "rip.log"],
   "the reply lists the archive's own tree")
staged = [f["path"] for f in tree["files"]]
res = CLIENT.post("/api/import/upload?target_dir=Archive%20Rip", data={"staged": json.dumps(staged)})
eq(res.status_code, 200, "the unpacked tree is imported through the same route", res.text[:300])
archive_album = os.path.join(library_root(MF), "Archive Rip")

check(os.path.isdir(folder_album) and os.path.isdir(archive_album),
      "both albums landed in the library",
      f"{os.path.isdir(folder_album)} {os.path.isdir(archive_album)}")
eq(sorted(os.listdir(archive_album)), sorted(os.listdir(folder_album)),
   "the archive's album holds what the folder's album holds")
eq(sorted(os.listdir(archive_album)),
   [".mlo_expected.json", "01 - One.wav", "02 - Two.wav", "rip.cue", "rip.log"],
   "…the tracks AND the rip's own .cue/.log")

for label, album in (("the folder", folder_album), ("the archive", archive_album)):
    # Both were handed a rip whose sheets name FOUR tracks and whose audio is
    # two of them: the album has to say so, from the sidecar alone.
    with open(os.path.join(album, ".mlo_expected.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    rows = manifest.get("tracks") or []
    eq(len(rows), 4, f"{label}'s album is marked against the 4 tracks its cue sheet names")
    eq([r.get("file") for r in rows], CUE_NAMES,
       f"{label}'s manifest names the files the cue sheet names")

# A zip that holds the album FOLDER keeps it: the tree a drop gave the archive
# is the tree the album gets, exactly as a dropped folder's is.
nested_zip = os.path.join(WORK, "nested-album.zip")
with zipfile.ZipFile(nested_zip, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in os.listdir(ALBUM_SRC):
        zf.write(os.path.join(ALBUM_SRC, name), f"The Album/{name}")
with open(nested_zip, "rb") as fh:
    res = CLIENT.post("/api/import/unpack", files={"file": ("nested-album.zip", fh.read())})
eq(res.status_code, 200, "an archive holding the album folder is unpacked", res.text[:200])
staged = [f["path"] for f in res.json()["files"]]
nested_dir = res.json()["dir"]
res = CLIENT.post("/api/import/upload?target_dir=Nested%20Rip", data={"staged": json.dumps(staged)})
eq(res.status_code, 200, "…and imported", res.text[:300])
eq(sorted(os.listdir(os.path.join(library_root(MF), "Nested Rip"))), ["The Album"],
   "…as the album folder the archive held, not flattened into the album")

# (c) the staged list may only name a folder THIS app unpacked.
res = CLIENT.post("/api/import/upload?target_dir=Escape",
                  data={"staged": json.dumps([os.path.join(MF, "..", "evil.wav")])})
eq(res.status_code, 400, "a staged path outside the unpack area is refused")
check("unpack area" in res.text, "…with the reason", res.text[:200])
res = CLIENT.post("/api/import/upload?target_dir=Escape",
                  data={"staged": json.dumps([os.path.join(ALBUM_SRC, "01 - One.wav")])})
eq(res.status_code, 400, "a staged path naming the user's own folder is refused too")
check(os.path.isfile(os.path.join(ALBUM_SRC, "01 - One.wav")),
      "…and the file it named is untouched")

# (d) the staging folder is the app's own, and the wizard can drop it.
check(os.path.basename(tree["dir"]).startswith("unpacked-"),
      "the unpacked tree lives in a folder this app made", tree["dir"])
check(os.path.normcase(os.path.dirname(tree["dir"])) ==
      os.path.normcase(os.path.realpath(mlo_root(MF))),
      "…inside the music folder's own state folder", tree["dir"])
# Every tree this script unpacks is discarded as the wizard discards it, so
# the last assertion below is about what a REFUSAL leaves, not about leftovers
# this script made itself.
res = CLIENT.post("/api/import/unpack/discard",
                  data={"dirs": [tree["dir"], nested_dir]})
eq(res.status_code, 200, "the wizard can discard them", res.text[:200])
eq(sorted(res.json()["removed"]), sorted([os.path.basename(tree["dir"]),
                                          os.path.basename(nested_dir)]),
   "…both unpacked trees it made")
check(not os.path.isdir(tree["dir"]), "…and they are gone")
res = CLIENT.post("/api/import/unpack/discard", data={"dirs": [os.path.dirname(ALBUM_SRC)]})
eq(res.json()["skipped"], [os.path.dirname(ALBUM_SRC).replace("\\", "/")],
   "a folder the app did NOT make is never removed")
check(os.path.isdir(os.path.dirname(ALBUM_SRC)), "…it is still there")

# --------------------------------------------------------------------------- #
# 4. an archive with no audio says so
# --------------------------------------------------------------------------- #
print("\n== an archive of scans is not an empty album ==")

scans_zip = os.path.join(WORK, "scans.zip")
with open(scans_zip, "wb") as fh:
    fh.write(zip_bytes([("cover.jpg", b"\xff\xd8\xff"), ("notes.txt", b"nothing")]))
with open(scans_zip, "rb") as fh:
    res = CLIENT.post("/api/import/unpack", files={"file": ("scans.zip", fh.read())})
eq(res.status_code, 200, "an audio-less archive is still unpacked", res.text[:200])
eq(res.json()["audio"], 0, "…and reports zero audio, which is what the wizard says out loud")
eq(sorted(f["relPath"] for f in res.json()["files"]), ["cover.jpg", "notes.txt"],
   "…with its files listed")
CLIENT.post("/api/import/unpack/discard", data={"dirs": [res.json()["dir"]]})

# A crafted archive through the ROUTE: the refusal is a 400 with the reason,
# and it leaves nothing behind in the staging area.
bad_zip = os.path.join(WORK, "bad.zip")
with open(bad_zip, "wb") as fh:
    fh.write(zip_bytes([("../../escape.wav", b"nope")]))
before = sorted(n for n in os.listdir(mlo_root(MF)) if n.startswith("unpacked-"))
with open(bad_zip, "rb") as fh:
    res = CLIENT.post("/api/import/unpack", files={"file": ("bad.zip", fh.read())})
eq(res.status_code, 400, "a crafted archive is refused by the route")
check("escape.wav" in res.text and ".." in res.text,
      "…naming the member and the escape", res.text[:200])
left = sorted(n for n in os.listdir(mlo_root(MF)) if n.startswith("unpacked-"))
eq(left, before, "…and no staging folder was left behind")
eq([n for n in os.listdir(mlo_root(MF)) if n.startswith("incoming-")], [],
   "…nor the uploaded archive itself")

res = CLIENT.post("/api/import/unpack", files={"file": ("notes.txt", b"not an archive")})
eq(res.status_code, 400, "a file that is not an archive is refused")
check(".zip" in res.text, "…listing the formats that ARE supported", res.text[:200])

# --------------------------------------------------------------------------- #
print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    for name in FAILS:
        print(f"  - {name}")
    sys.exit(1)
print("all archive-import checks passed")
