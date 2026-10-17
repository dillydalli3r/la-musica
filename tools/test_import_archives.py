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
    what keeps the route from placing any file on the server into the library;
  * a folder or file the USER dragged onto the app (or picked in the wizard's
    folder dialog) is COPIED into the library: the original's bytes, names and
    mtimes are untouched, because the app's own staging is the only thing it
    owns — while a path the APP staged (`<music>/.mlo/…`, or a finished
    download) is still MOVED and drained, so staging leaves no duplicate;
  * a track ADDED to an album the library already holds
    (`POST /api/import/upload?album=…`, the wizard's Match step "Add tracks…")
    fills THAT album: the path is used as-is, so no `(2)` folder is opened
    beside it, no name is guessed from tags, and a name the album holds keeps
    the album's own bytes (`kept`); a path outside the music folder is refused.

Nothing here touches the network and no external tool is required: the
fixtures are crafted archives (zip and tar by hand, `.7z` only when this host
has 7-Zip) and the rip is WAV + a cue sheet. The music folder is a temp
directory with both stores redirected into it, and the routes are driven
through the app's own HTTP surface (server.main.app) exactly as the wizard
drives them.

Run:  python tools/test_import_archives.py   (exit 0 pass, 1 fail)
"""
import atexit
import hashlib
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
from mlo.config import normalize_config  # noqa: E402
from mlo.grader import _grade_album  # noqa: E402
from mlo.paths import downloads_dir, library_root, mlo_root  # noqa: E402
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
   [".mb_expected", "01 - One.wav", "02 - Two.wav", "rip.cue", "rip.log"],
   "…the tracks AND the rip's own .cue/.log")

for label, album in (("the folder", folder_album), ("the archive", archive_album)):
    # Both were handed a rip whose sheets name FOUR tracks and whose audio is
    # two of them: the album has to say so, from the sidecar alone.
    with open(os.path.join(album, ".mb_expected"), encoding="utf-8") as fh:
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
# 3c. adding tracks to an album the library already holds
# --------------------------------------------------------------------------- #
# `POST /api/import/upload` takes an `album` path — the wizard's Match step,
# "Add tracks…" (spec R396). The caller already knows which album the files
# belong to, so the path is used AS-IS: no name is guessed, no `(2)` folder is
# opened, and the arriving audio FILLS the album (`merge_into_album`'s rule —
# a file the album already holds is kept, never replaced). Without it, adding
# one track to an album the library lists could only be done by re-importing
# the whole folder through a name the tags had to agree with.
print("\n== adding tracks to an album the library already holds ==")

add_src = os.path.join(WORK, "add-src")
os.makedirs(add_src, exist_ok=True)
first = os.path.join(add_src, "03 - Added.wav")
make_wav(first, 1.0)
with open(first, "rb") as fh:
    res = CLIENT.post("/api/import/upload",
                      params={"target_dir": os.path.basename(folder_album),
                              "album": folder_album},
                      files=[("files", ("03 - Added.wav", fh.read()))])
eq(res.status_code, 200, "a track added to an existing album is accepted", res.text[:300])
body = res.json()
eq(body.get("merged"), True, "the reply says the album was filled")
eq(body.get("saved"),
   [os.path.join(folder_album, "03 - Added.wav").replace("\\", "/")],
   "…and names the file it placed")
check("03 - Added.wav" in os.listdir(folder_album),
      "the file landed IN that album, not in a folder named after it",
      os.listdir(folder_album))
check(not os.path.isdir(os.path.join(library_root(MF), "Folder Rip (2)")),
      "…and no (2) folder was opened beside it")
own_before = {name: hashlib.sha1(open(os.path.join(folder_album, name), "rb").read()).hexdigest()
              for name in ("01 - One.wav", "02 - Two.wav", "rip.cue", "rip.log")}
after_names = sorted(os.listdir(folder_album))
check(all(name in after_names for name in own_before),
      "the album's own files are all still there", after_names)

# The same name again is KEPT, not replaced: the album's copy keeps its bytes.
second = os.path.join(add_src, "03 - Added (different).wav")
make_wav(second, 2.0)
before = hashlib.sha1(open(os.path.join(folder_album, "03 - Added.wav"), "rb").read()).hexdigest()
with open(second, "rb") as fh:
    res = CLIENT.post("/api/import/upload",
                      params={"target_dir": os.path.basename(folder_album),
                              "album": folder_album},
                      files=[("files", ("03 - Added.wav", fh.read()))])
eq(res.status_code, 200, "re-adding a name the album holds is accepted", res.text[:300])
eq(res.json().get("kept"),
   [os.path.join(folder_album, "03 - Added.wav")],
   "…and reported as kept")
eq(hashlib.sha1(open(os.path.join(folder_album, "03 - Added.wav"), "rb").read()).hexdigest(),
   before, "…with the album's own bytes, not the arriving ones")
eq({name: hashlib.sha1(open(os.path.join(folder_album, name), "rb").read()).hexdigest()
    for name in own_before}, own_before,
   "…and none of the album's own files was rewritten by either add")

# The album path is a containment guard, not free text: outside the music
# folder is refused outright.
res = CLIENT.post("/api/import/upload",
                  params={"target_dir": "Elsewhere",
                          "album": os.path.join(MF, "..", "Elsewhere")},
                  files=[("files", ("x.wav", open(first, "rb").read()))])
eq(res.status_code, 400, "an album path outside the music folder is refused")
check("outside music folder" in res.text, "…with the reason", res.text[:200])
res = CLIENT.post("/api/import/upload",
                  params={"target_dir": "Elsewhere",
                          "album": os.path.join(library_root(MF), "Nowhere")},
                  files=[("files", ("x.wav", open(first, "rb").read()))])
eq(res.status_code, 404, "a path that is not an album is refused too", res.text[:200])

# --------------------------------------------------------------------------- #
# 3d. TWO BATCHES, ONE ALBUM NAME: neither batch overwrites the other
# --------------------------------------------------------------------------- #
# The wizard's worklist stages several batches and imports them one after
# another, each album through the SAME POST /api/import/upload. Two batches
# that resolve to the SAME album name — two discs, two editions, or two uploads
# of one album — used to land in one folder, and `_place_staged` moved the
# second batch's files onto the first's with os.replace: the first batch's
# audio was GONE, silently. The second upload is now a DIFFERENT album that
# happens to share the name, so it gets a folder of its own and both survive.
print("\n== two batches, one album name: both land, neither is overwritten ==")

BATCHES = os.path.join(WORK, "batches")
batch_one = os.path.join(BATCHES, "one", "Colliding Album.wav")
batch_two = os.path.join(BATCHES, "two", "Colliding Album.wav")
os.makedirs(os.path.dirname(batch_one), exist_ok=True)
os.makedirs(os.path.dirname(batch_two), exist_ok=True)
make_wav(batch_one, 0.05)
make_wav(batch_two, 0.25)          # the same track NAME, different bytes
first_bytes = open(batch_one, "rb").read()
second_bytes = open(batch_two, "rb").read()
check(first_bytes != second_bytes, "the fixture's two tracks differ byte for byte")


def upload_batch(path, name="Colliding Album"):
    """One batch's album through the route the wizard calls, exactly as the
    worklist does: a multipart upload under the album name the batch carries."""
    with open(path, "rb") as fh:
        return CLIENT.post("/api/import/upload", params={"target_dir": name},
                           files=[("files", ("01 - track.wav", fh.read()))])


res_a = upload_batch(batch_one)
eq(res_a.status_code, 200, "the first batch's album imports", res_a.text[:200])
album_a = res_a.json()["album_path"]
eq(os.path.basename(album_a), "Colliding Album", "…under the name it was given")
eq(res_a.json().get("collided"), False, "…and it collided with nothing")
# R428: the reply says whether THIS call created the album folder — the
# wizard's Cancel discards exactly what its own staging made, so "the
# folder is yours to remove" has to be the server's fact, not a guess.
eq(res_a.json().get("created"), True, "…and it says this call CREATED the album")

res_b = upload_batch(batch_two)
eq(res_b.status_code, 200, "the second batch's album imports too", res_b.text[:200])
album_b = res_b.json()["album_path"]
eq(os.path.basename(album_b), "Colliding Album (2)",
   "…into a folder of its own: the taken name is deduplicated, as the bulk queue does")
eq(res_b.json().get("collided"), True, "…and the reply says the name was taken")
check(_pathish(album_b) != _pathish(album_a), "the two albums are different folders")

one = os.path.join(album_a, "01 - track.wav")
two = os.path.join(album_b, "01 - track.wav")
check(os.path.isfile(one) and os.path.isfile(two), "both tracks are on disk")
eq(open(one, "rb").read(), first_bytes,
   "the FIRST batch's track is byte for byte the file it uploaded")
eq(open(two, "rb").read(), second_bytes,
   "…and the second batch's is too: neither replaced the other")
eq(sorted(n for n in os.listdir(library_root(MF)) if n.startswith("Colliding")),
   ["Colliding Album", "Colliding Album (2)"],
   "…and the library gained exactly two albums, not one fused and not one lost")

# Both are REAL albums, not orphan folders: the library lists each of them with
# its own track, which is what "both batches imported" means.
from mlo.config import load_config as _load_config  # noqa: E402
from server import library as _lib_mod  # noqa: E402
from server import tagcache as _tagcache  # noqa: E402

_tagcache.invalidate_all()
_albums = {os.path.normcase(os.path.abspath(str(a.get("path") or ""))): a
           for artist in _lib_mod.build_library(_load_config()).get("artists", [])
           for a in artist.get("albums", [])}
for _label, _album in (("the first", album_a), ("the second", album_b)):
    _row = _albums.get(os.path.normcase(os.path.abspath(_album)))
    check(_row is not None, f"{_label} batch's album is a library album", _album)
    eq(sorted(os.path.basename(t.get("file") or "")
              for t in (_row or {}).get("tracks") or []),
       ["01 - track.wav"], f"…{_label} batch's album holds its own track")

# The SAME upload again is not a collision: a retried batch lands on the
# byte-identical copies it made, so it stays ONE album. This is the deliberate
# replace `_place_staged` was written for — it must not become " (3)".
res_retry = upload_batch(batch_two)
eq(res_retry.status_code, 200, "a retried batch imports", res_retry.text[:200])
eq(os.path.basename(res_retry.json()["album_path"]), "Colliding Album (2)",
   "…into the folder it already made, not a third one")
eq(res_retry.json().get("collided"), False, "…and that is not a collision")
eq(res_retry.json().get("created"), False,
   "…and a folder its own earlier attempt made is not 'created' by this one")
eq(sorted(n for n in os.listdir(library_root(MF)) if n.startswith("Colliding")),
   ["Colliding Album", "Colliding Album (2)"],
   "still exactly two albums for the two batches")

# A tree staged by the SERVER (`staged=`, the wizard's unpacked archive)
# follows the same rule: an album name already taken gets a folder of its own
# rather than replacing the albums that are there.
third_wav = os.path.join(BATCHES, "three", "01 - track.wav")
os.makedirs(os.path.dirname(third_wav), exist_ok=True)
make_wav(third_wav, 0.6)
third_bytes = open(third_wav, "rb").read()
third_zip = os.path.join(BATCHES, "third.zip")
with open(third_zip, "wb") as fh:
    fh.write(zip_bytes([("01 - track.wav", third_bytes)]))
with open(third_zip, "rb") as fh:
    res = CLIENT.post("/api/import/unpack", files={"file": ("third.zip", fh.read())})
eq(res.status_code, 200, "the third batch's archive is unpacked", res.text[:200])
third_tree = res.json()
res_staged = CLIENT.post("/api/import/upload", params={"target_dir": "Colliding Album"},
                         data={"staged": json.dumps([f["path"] for f in third_tree["files"]])})
eq(res_staged.status_code, 200, "…and imported through the same route", res_staged.text[:200])
eq(os.path.basename(res_staged.json()["album_path"]), "Colliding Album (3)",
   "…into a THIRD folder, never onto the two that are there")
eq(res_staged.json().get("collided"), True, "…and the reply says the name was taken")
eq(open(os.path.join(res_staged.json()["album_path"], "01 - track.wav"), "rb").read(),
   third_bytes, "…with the staged copy's own bytes")
eq(open(one, "rb").read(), first_bytes, "…and the first album is still untouched")
CLIENT.post("/api/import/unpack/discard", data={"dirs": [third_tree["dir"]]})

# The function's own rule, called directly: it never replaces a differing file.
# (The route above deduplicates before it gets here; this keeps the guarantee
# true of `_place_staged` itself, whoever calls it.)
from fastapi import HTTPException as _HTTPException  # noqa: E402

with tempfile.TemporaryDirectory(prefix="place-", dir=WORK) as place_stage:
    src = os.path.join(place_stage, "01 - track.wav")
    with open(src, "wb") as fh:
        fh.write(second_bytes)
    try:
        mlo_main._place_staged([("01 - track.wav", src)], album_a)
    except _HTTPException as e:
        check("different file" in str(e.detail) and "01 - track.wav" in str(e.detail),
              "placing onto a DIFFERENT name is refused, naming the collision",
              str(e.detail))
    else:
        check(False, "placing onto a different name must be refused, not replaced")
    check(os.path.isfile(src), "…and the staged copy is left whole")
    eq(open(os.path.join(album_a, "01 - track.wav"), "rb").read(), first_bytes,
       "…with the album's own bytes untouched")
    # …while a file it already holds byte for byte is simply dropped: a retry
    # repeats its own upload, it does not duplicate it.
    again = os.path.join(place_stage, "copy.wav")
    shutil.copyfile(one, again)
    mlo_main._place_staged([("01 - track.wav", again)], album_a)
    check(not os.path.exists(again), "an identical copy is dropped, not kept beside")
    eq(open(one, "rb").read(), first_bytes, "…and the album's copy is still the same bytes")

# --------------------------------------------------------------------------- #
# 3e. WHO supplied the folder decides whether it MOVES or is COPIED. The
#     wizard hands `/api/import/ingest` the path a user dragged onto the app
#     (or picked in its native folder dialog), and that folder is the USER's:
#     the album is COPIED into the library and the original stays exactly where
#     it was — every byte, every mtime, every folder. Only the app's OWN
#     staging (<music>/.mlo: downloads, incomplete, the unpacked-* trees it
#     makes) is drained, because a staging file left behind is a duplicate the
#     app made and nobody asked for.
# --------------------------------------------------------------------------- #
print("\n== a dragged folder is copied; the app's own staging is drained ==")


def _tree_state(root):
    """{rel path: (sha256, size, mtime)} for every file under *root*, plus the
    directories' own mtimes: the whole tree's attendance, not one file's."""
    files, dirs = {}, {}
    for base, names, fnames in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            dirs[os.path.relpath(p, root).replace("\\", "/")] = os.path.getmtime(p)
        for n in sorted(fnames):
            p = os.path.join(base, n)
            rel = os.path.relpath(p, root).replace("\\", "/")
            with open(p, "rb") as fh:
                files[rel] = (hashlib.sha256(fh.read()).hexdigest(),
                              os.path.getsize(p), os.path.getmtime(p))
    return files, dirs


DRAGGED = os.path.join(WORK, "dragged", "Dragged Album")
os.makedirs(os.path.join(DRAGGED, "Scans"), exist_ok=True)
for name in ("01 - One.wav", "02 - Two.wav"):
    make_wav(os.path.join(DRAGGED, name), 0.05)
for rel, blob in (("cover.jpg", b"\xff\xd8\xff" * 40),
                  ("notes.txt", b"the user's own notes\n"),
                  ("Scans/back.jpg", b"\xff\xd8\xff" * 20)):
    with open(os.path.join(DRAGGED, *rel.split("/")), "wb") as fh:
        fh.write(blob)
dragged_before, dragged_dirs_before = _tree_state(DRAGGED)

# exactly what a desktop OS drop does: the shell hands PATHS over, the wizard
# scans the folder and then commits it through the ingest route
# (ImportWizard.handleDropPaths → api.importIngest).
res = CLIENT.post("/api/import/scan", params={"path": DRAGGED})
eq(res.status_code, 200, "the dragged folder is scanned", res.text[:200])
eq(res.json()["root"], os.path.realpath(DRAGGED).replace("\\", "/"),
   "…from the path the user dropped")
res = CLIENT.post("/api/import/ingest",
                  params={"source": DRAGGED, "target": "Dragged Album"})
eq(res.status_code, 200, "the dragged folder is ingested", res.text[:300])
eq(res.json().get("copied"), True, "the reply says the source was COPIED, not moved")
dragged_album = os.path.join(library_root(MF), "Dragged Album")
eq(os.path.normcase(res.json()["path"]),
   os.path.normcase(dragged_album.replace("\\", "/")), "the album is under <library>")
check(os.path.isdir(DRAGGED), "the user's folder is STILL THERE")
dragged_after, dragged_dirs_after = _tree_state(DRAGGED)
eq(dragged_after, dragged_before,
   "every source file is byte-identical — same sha256, size and mtime")
eq(dragged_dirs_after, dragged_dirs_before, "…and every source folder kept its mtime")
lib_files, _lib_dirs = _tree_state(dragged_album)
eq(lib_files, dragged_before, "the library copy holds exactly those bytes")
eq(sorted(lib_files), sorted(dragged_before),
   "…the audio AND its sidecars (cover.jpg, notes.txt, Scans/back.jpg)")
# The copy is a real album the app can read and grade, not a folder of bytes.
_grade = _grade_album(dragged_album, "lrc", normalize_config({"music_folder": MF}))
check(isinstance(_grade, dict) and "issues" in _grade,
      "the copied album is gradable", str(_grade)[:200])
check(any(t.get("file") == "01 - One.wav" for t in _grade.get("tracks") or []),
      "…and the grader lists the copied track", str(_grade.get("tracks"))[:200])

# A single FILE the shell handed over is copied the same way.
SOLO_DIR = os.path.join(WORK, "dragged-solo")
SOLO = os.path.join(SOLO_DIR, "07 - Solo.wav")
os.makedirs(SOLO_DIR, exist_ok=True)
make_wav(SOLO, 0.05)
solo_before, _ = _tree_state(SOLO_DIR)
res = CLIENT.post("/api/import/ingest", params={"source": SOLO, "target": "Dragged Solo"})
eq(res.status_code, 200, "a dragged single file is ingested", res.text[:300])
eq(res.json().get("copied"), True, "…and the reply says it was copied")
check(os.path.isfile(SOLO), "the user's file is still there")
eq(_tree_state(SOLO_DIR)[0], solo_before, "…byte-identical, mtime included")

# The app's OWN staging keeps its drain: a tree this app made under
# <music>/.mlo is MOVED into the library and leaves nothing behind.
STAGED_SRC = os.path.join(mlo_root(MF), "unpacked-drain-me", "Staged Album")
os.makedirs(STAGED_SRC, exist_ok=True)
make_wav(os.path.join(STAGED_SRC, "01 - Staged.wav"), 0.05)
res = CLIENT.post("/api/import/ingest",
                  params={"source": STAGED_SRC, "target": "Staged Album"})
eq(res.status_code, 200, "a staged folder is ingested", res.text[:300])
eq(res.json().get("copied"), False, "the reply says the app-staged tree was MOVED")
check(not os.path.exists(STAGED_SRC), "…and the staged tree is gone: no leftover")
check(os.path.isfile(os.path.join(library_root(MF), "Staged Album", "01 - Staged.wav")),
      "…its file is in the library")

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
