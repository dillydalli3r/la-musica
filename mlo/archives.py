"""Archives: what a safe member is, and the one extractor per format.

Two callers share every rule here, which is the point of the module:

  * ``extract`` — a USER's archive (the import wizard's drop/pick, and a path
    the desktop shell hands over). Untrusted input: an absolute path, a ``..``
    member, a link or a device node is refused with the reason, and an archive
    whose format has no available extractor says so instead of unpacking half
    of it. A refusal always happens BEFORE any byte is written, so nothing can
    land outside the destination.
  * ``extract_installer`` — the tool installer (mlo/fetchdeps.py), which fetches
    release assets this app trusts and has its own extra shapes (.deb, lzip,
    NSIS installers). It keeps its own looser policy — a Debian package's
    data.tar carries absolute symlinks into /usr/share/doc — because that is
    what installing a compiler toolchain needs, and it is never fed a file the
    user chose.

Nothing here shells out to an unvetted program: ``7z`` (7-Zip, or a compatible
build such as NanaZip) is looked up on PATH and in 7-Zip's usual install
directories, and when a format needs it and it is missing the answer is an
explicit error naming the format — never a silent half-extraction.
"""
import ctypes
import lzma
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile

from .subproc import run_tool


class ArchiveError(RuntimeError):
    """An archive could not be unpacked; the message says why."""


class UnsafeArchiveError(ArchiveError):
    """An archive holds an entry that may not be written (see member_problem)."""


class NoExtractorError(ArchiveError):
    """No available extractor can read this archive."""


# ----------------------------------------------------------------------
# What counts as an archive
# ----------------------------------------------------------------------
# The formats the IMPORT path accepts: the suffix (longest match first) and the
# family that unpacks it. `.rar` is in the table but read through 7-Zip only —
# there is no RAR decoder in the standard library, so on a host without 7-Zip
# the answer is NoExtractorError rather than a guess.
ARCHIVE_FORMATS = {
    ".tar.gz": "tar",
    ".tar.bz2": "tar",
    ".tar.xz": "tar",
    ".tgz": "tar",
    ".tbz2": "tar",
    ".txz": "tar",
    ".tar": "tar",
    ".zip": "zip",
    ".7z": "sevenzip",
    ".rar": "sevenzip",
}

# What a refusal names, in the user's words (the wizard shows this string).
SUPPORTED_ARCHIVES = (".zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, "
                      ".tar.xz/.txz, .7z and .rar")

_NEEDS_7Z = {
    "sevenzip": "this format is read with 7-Zip — install 7-Zip (or put 7z/7za "
                "on PATH) and try again",
}


def archive_suffix(name: str) -> str:
    """The archive extension of a release asset, compression chain included.

    `os.path.splitext` alone cuts "….tar.gz" down to ".gz", and a temp file
    named "*.gz" is not recognisable as a tarball to extract_installer — it fell
    through to the "run it as an installer" branch and died with rc=126 on the
    first Linux tarball this installer ever fetched.
    """
    lower = name.lower()
    for suffix in (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.lz", ".tgz", ".tar",
                   ".zip", ".7z", ".exe", ".deb", ".phar"):
        if lower.endswith(suffix):
            return suffix
    return os.path.splitext(name)[1]


def import_kind(name: str) -> str | None:
    """Which family unpacks `name` for an IMPORT, or None when none does.

    Longest suffix first, so ".tar.gz" is never read as a ".gz" nothing here
    handles.
    """
    lower = name.lower()
    for suffix in sorted(ARCHIVE_FORMATS, key=len, reverse=True):
        if lower.endswith(suffix):
            return ARCHIVE_FORMATS[suffix]
    return None


def is_archive(name: str) -> bool:
    """Would the import path accept `name` as an archive?"""
    return import_kind(name) is not None


# ----------------------------------------------------------------------
# The safety rule
# ----------------------------------------------------------------------
# Windows keeps a colon out of a filename, so a member naming a drive is an
# absolute path spelled the other way.
_DRIVE = re.compile(r"^[A-Za-z]:")


def member_problem(name: str) -> str | None:
    """Why a member name may not be written, or None when it is safe.

    One rule for every format, applied BEFORE anything is extracted:

      * an empty name, an absolute path (POSIX "/…", a Windows drive, a UNC
        "\\\\server\\share") — writes outside the destination;
      * a ".." segment anywhere in the path — the same escape written by hand;
      * a NUL byte, which would truncate the name at the syscall.
    """
    if not name or "\0" in name:
        return "empty or unreadable name"
    path = name.replace("\\", "/")
    if path.startswith("/") or path.startswith("//") or _DRIVE.match(path):
        return "absolute path"
    if ".." in [p for p in path.split("/")]:
        return "path escape ('..')"
    return None


def _refuse(label: str, name: str, why: str) -> UnsafeArchiveError:
    return UnsafeArchiveError(f"{label}: refused — {why}: {name!r}")


def _verify_tree(dest_dir: str, label: str) -> None:
    """Nothing but real files and folders came out of the archive.

    The zip and tar readers refuse links and device nodes by reading the
    archive's own index; 7-Zip's index is not readable that way, so this walks
    what actually landed. Called with the extraction in place: a link or a
    device node means the archive was refused, whether or not it would have
    been followed.
    """
    for root, dirs, files in os.walk(dest_dir, followlinks=False):
        for name in list(dirs) + list(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, dest_dir).replace("\\", "/")
            if os.path.islink(full):
                raise _refuse(label, rel, "symbolic link")
            try:
                mode = os.lstat(full).st_mode
            except OSError:
                continue
            if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or \
                    stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
                raise _refuse(label, rel, "device node")


# ----------------------------------------------------------------------
# 7-Zip: the shared locator, lister and runner
# ----------------------------------------------------------------------
def find_7z() -> str | None:
    """7-Zip (or a compatible build) on this host, or None."""
    path = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    if path:
        return path
    for candidate in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def windows_short_path(path: str) -> str | None:
    """8.3 short path (space-free) for NSIS /D=, or None on failure."""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(
            os.path.abspath(path), buf, len(buf)
        )
        if 0 < n < len(buf):
            return buf.value
    except Exception:
        pass
    return None


def extract_with_7z(sevenz: str, archive_path: str, dest_dir: str) -> None:
    result = run_tool(
        [sevenz, "x", "-y", f"-o{dest_dir}", archive_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=180, check=False,
    )
    if result.returncode != 0:
        raise ArchiveError(f"7-Zip extraction failed (rc={result.returncode})")


def list_with_7z(sevenz: str, archive_path: str) -> list[tuple[str, str]]:
    """(path, attributes) per member, from 7-Zip's own technical listing.

    A user archive is validated before it is extracted, and for `.7z`/`.rar`
    this listing IS the index: 7-Zip cannot be asked to hand one over in a
    shape the standard library reads. `-slt` prints one `Key = Value` block per
    member after the archive's own header block, which is what makes `Path`
    readable — the human listing truncates and wraps paths instead — and
    `-sccUTF-8` decodes member names as the UTF-8 they are stored as, not in
    whatever console codepage this host happens to be using.
    """
    result = run_tool([sevenz, "l", "-slt", "-sccUTF-8", "--", archive_path],
                      capture_output=True, text=True, timeout=300, check=False)
    text = result.stdout or ""
    if result.returncode != 0 or "----------" not in text:
        detail = (result.stderr or "").strip().splitlines()
        raise NoExtractorError(
            f"7-Zip could not list this archive"
            + (f": {detail[0]}" if detail else ""))
    members = []
    keys: dict[str, str] = {}
    started = False
    for line in text.splitlines():
        if line.startswith("----------"):
            started = True
            continue
        if not started:
            continue
        if not line.strip():
            if keys:
                if "Path" in keys:
                    members.append((keys["Path"], keys.get("Attributes", "")))
                keys = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            keys[key.strip()] = value.strip()
    if keys.get("Path"):
        members.append((keys["Path"], keys.get("Attributes", "")))
    return members


def _check_sevenzip(sevenz: str, archive_path: str, label: str) -> None:
    """Refuse a 7-Zip-readable archive whose members may not be written."""
    for path, attrs in list_with_7z(sevenz, archive_path):
        why = member_problem(path)
        if why:
            raise _refuse(label, path, why)
        # 7-Zip marks a stored symbolic link with an L in the attribute run
        # ("...L"). A device node is not something 7-Zip creates on Windows at
        # all, and _verify_tree walks whatever did land.
        if attrs.startswith("L") or attrs[4:5] == "L":
            raise _refuse(label, path, "symbolic link")


def _sevenzip_for(kind: str, label: str) -> str:
    sevenz = find_7z()
    if not sevenz:
        raise NoExtractorError(f"{label}: {_NEEDS_7Z[kind]}")
    return sevenz


# ----------------------------------------------------------------------
# tar / lzip / zstd / deb / installer shapes (mlo.fetchdeps)
# ----------------------------------------------------------------------
def extract_tar(source, dest_dir: str, *, allow_absolute_links: bool = False) -> None:
    """Extract a tarball — a path, or an open file object — into *dest_dir*.

    tarfile restores each entry's mode, which is what makes the unpacked binary
    executable; `filter="data"` (3.12+) keeps that while refusing entries that
    would escape dest_dir or carry device nodes, the same guarantee zipfile
    gives. Compression is detected from the content (xz, gz, bz2), so a temp
    file's name decides nothing.

    `allow_absolute_links` relaxes exactly one of those rules, for the one
    archive shape that needs it: a Debian package's data.tar carries absolute
    symlinks into /usr/share/doc beside its binaries, and the data filter
    refuses those outright (measured on libjpeg-turbo's .deb). The `tar` filter
    keeps what matters — nothing is written outside *dest_dir* and no relative
    link may point out of it — and only stores the absolute link as the link it
    says it is. Nothing follows it: the install copies the binaries folder and
    the lib folder, and a dangling link beside them is never traversed.
    """
    with tarfile.open(source) as tf:
        try:
            tf.extractall(dest_dir,
                          filter="tar" if allow_absolute_links else "data")
        except TypeError:          # Python < 3.12: no extraction filters
            tf.extractall(dest_dir)


_LZIP_MAGIC = b"LZIP"


def lzip_plain_bytes(archive_path: str, dest_path: str) -> None:
    """Write the decompressed contents of an lzip file to *dest_path*.

    libjxl publishes its static Linux build as `.tar.lz` and nothing else (see
    LINUX_BINARIES), and **Python's lzma module cannot read lzip as a
    container**: `lzma.open` knows .xz and the LZMA-alone header, so handing it
    a lzip file fails with "Input format not supported by decoder" (measured
    against the v0.12.0 asset). What lzip wraps is a raw LZMA1 stream though —
    its own 6-byte header carries the magic, a version and the dictionary size,
    and a 20-byte trailer follows the compressed data — so that is what is
    decoded here, exactly as the `lzip` binary would: FORMAT_RAW with the
    dictionary size from the header and lzip's fixed lc/lp/pb (3/0/2). No lzip
    executable has to exist on the host for this, which is what makes the
    install work in a container.

    A lzip file is a SEQUENCE of members, and the libjxl asset really has two:
    the tarball, then a 44-byte member holding tar's two empty end blocks
    (measured). Each member is decoded in turn and the next one found after the
    previous one's trailer, so the result is the whole tar stream. Anything
    that is not lzip raises with the reason instead of leaving half a tarball
    behind.
    """
    with open(archive_path, "rb") as fh:
        data = fh.read()
    with open(dest_path, "wb") as out:
        pos = 0
        while pos < len(data):
            if data[pos:pos + 4] != _LZIP_MAGIC:
                raise ArchiveError(
                    f"{os.path.basename(archive_path)} is not an lzip archive "
                    f"(no LZIP signature at byte {pos})")
            code = data[pos + 5]
            size = 1 << (code & 0x1F)
            try:
                member = lzma.LZMADecompressor(
                    format=lzma.FORMAT_RAW,
                    filters=[{"id": lzma.FILTER_LZMA1,
                              "dict_size": size - (size // 16) * ((code >> 5) & 7),
                              "lc": 3, "lp": 0, "pb": 2}])
                out.write(member.decompress(data[pos + 6:]))
            except lzma.LZMAError as e:
                raise ArchiveError(
                    f"could not decompress the lzip member at byte {pos}: {e}") from e
            if not member.eof:
                raise ArchiveError(
                    f"the lzip member at byte {pos} is truncated")
            # The trailer (crc, sizes) ends where the next member begins; the
            # decoder hands back everything after the compressed stream, so the
            # next magic is looked up from there.
            tail = data.find(_LZIP_MAGIC, len(data) - len(member.unused_data))
            pos = tail if tail > pos else len(data)


def zstd_decompress(src: str, dst: str) -> None:
    """Copy a zstd-compressed file to *dst* plain, or say what is missing.

    dpkg can compress a .deb's data member with zstd (the amd64/arm64
    libjpeg-turbo packages ship xz, but the format is dpkg's own choice), and
    Python's stdlib only learned to read zstd in 3.14 — so this names the
    missing piece rather than writing a tar nothing can read.
    """
    try:
        import zstandard
    except ImportError as e:
        raise ArchiveError(
            "this .deb's data member is zstd-compressed and this Python has no "
            "zstd reader — install the `zstandard` package and retry") from e
    with open(src, "rb") as fh, open(dst, "wb") as out:
        with zstandard.ZstdDecompressor().stream_reader(fh) as reader:
            shutil.copyfileobj(reader, out)


def extract_deb(archive_path: str, dest_dir: str, log) -> None:
    """Extract a Debian package's file tree without dpkg.

    libjpeg-turbo publishes its Linux builds as .deb and nothing else (see
    LINUX_BINARIES), and dpkg-deb is not on every host — and not on Windows at
    all — so the wrapper is read here directly. It is a Unix `ar` archive: a
    fixed 60-byte header per member (name, mtime, owner, mode, size in plain
    decimal), odd-sized members padded by one byte, and only `data.tar.*` is the
    file tree (`debian-binary` and `control.tar.*` are metadata). The data
    member is itself a tarball and goes through extract_tar like every other
    asset, which is also what restores the file modes a package's binaries need
    to be runnable.
    """
    fd, data_file = tempfile.mkstemp(prefix="mlo_deb_")
    member = ""
    try:
        with open(archive_path, "rb") as fh, os.fdopen(fd, "wb") as out:
            if fh.read(8) != b"!<arch>\n":
                raise ArchiveError(
                    f"{os.path.basename(archive_path)} is not a Debian package "
                    f"(no ar signature)")
            while True:
                header = fh.read(60)
                if len(header) < 60:
                    raise ArchiveError(
                        f"{os.path.basename(archive_path)} holds no data.tar member")
                member = header[:16].decode("ascii", "replace").strip().rstrip("/")
                try:
                    size = int(header[48:58].decode("ascii").strip())
                except ValueError:
                    raise ArchiveError(
                        f"unreadable ar member header for {member!r}")
                if not member.startswith("data.tar"):
                    fh.seek(size + (size % 2), os.SEEK_CUR)
                    continue
                log(f"  package data: {member} ({size} bytes)")
                remaining = size
                while remaining:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        raise ArchiveError(f"truncated ar member {member!r}")
                    out.write(chunk)
                    remaining -= len(chunk)
                break
        if member.endswith(".zst"):
            plain = data_file + ".tar"
            try:
                zstd_decompress(data_file, plain)
                extract_tar(plain, dest_dir, allow_absolute_links=True)
            finally:
                try:
                    os.remove(plain)
                except OSError:
                    pass
        else:
            extract_tar(data_file, dest_dir, allow_absolute_links=True)
    finally:
        try:
            os.remove(data_file)
        except OSError:
            pass


def extract_installer(archive_path: str, dest_dir: str, log) -> None:
    """Extract a RELEASE ASSET (zip / tar / lzip / deb / 7z / NSIS) into dest_dir.

    The tool installer's entry point. Its archives come from the projects this
    app installs, not from the user, and two of its shapes have no counterpart
    in `extract`: a .deb's data.tar, and a Windows NSIS installer, which is not
    an archive at all (7-Zip unpacks it; without 7-Zip it is run silently).
    """
    lower = archive_path.lower()

    if lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        # Linux release assets are tarballs (oxipng) as often as zips.
        extract_tar(archive_path, dest_dir)
        return

    if lower.endswith(".tar.lz"):
        # lzip is a decompression of its own (see lzip_plain_bytes), so the
        # members are written out as a plain tarball first and that goes through
        # the same extraction as the others.
        fd, plain = tempfile.mkstemp(prefix="mlo_lz_", suffix=".tar")
        os.close(fd)
        try:
            lzip_plain_bytes(archive_path, plain)
            extract_tar(plain, dest_dir)
        finally:
            try:
                os.remove(plain)
            except OSError:
                pass
        return

    if lower.endswith(".deb"):
        extract_deb(archive_path, dest_dir, log)
        return

    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
            return
        except Exception:
            # Some release zips (libjxl) use methods zipfile cannot read;
            # fall through to 7-Zip if it is available.
            sevenz = find_7z()
            if not sevenz:
                raise ArchiveError(
                    "This zip uses a compression method Python cannot read "
                    "and 7-Zip is not installed. Install 7-Zip and retry."
                )
            extract_with_7z(sevenz, archive_path, dest_dir)
            return

    if lower.endswith(".7z"):
        sevenz = find_7z()
        if not sevenz:
            raise ArchiveError("Extracting .7z archives requires 7-Zip.")
        extract_with_7z(sevenz, archive_path, dest_dir)
        return

    # NSIS installer.
    sevenz = find_7z()
    if sevenz:
        extract_with_7z(sevenz, archive_path, dest_dir)
        return

    log("  7-Zip not found - falling back to silent install of the installer.")
    target = windows_short_path(dest_dir) or dest_dir
    if " " in target:
        raise ArchiveError(
            "Cannot silently install: temporary path contains spaces and "
            "7-Zip is unavailable. Install 7-Zip and retry."
        )
    # /D= is passed through cmd.exe unquoted, so shell metacharacters in the
    # path would either break the command or inject into it.
    if any(ch in target for ch in '&^|<>"'):
        raise ArchiveError(
            "Cannot silently install: temporary path contains shell "
            "metacharacters and 7-Zip is unavailable."
        )
    # /D must be the last argument and unquoted.
    result = run_tool(
        f'"{archive_path}" /S /D={target}',
        shell=True, timeout=300, capture_output=True,
    )
    if result.returncode != 0:
        raise ArchiveError(f"silent install failed (rc={result.returncode})")


# ----------------------------------------------------------------------
# A user's archive
# ----------------------------------------------------------------------
def extract(archive_path: str, dest_dir: str, *, log=print) -> None:
    """Unpack a USER-SUPPLIED archive into *dest_dir*, or refuse it.

    Every member is inspected before a byte is written: an absolute path, a
    ".." escape, a symbolic/hard link or a device node is an UnsafeArchiveError
    naming the member, and the destination is left as it was (the caller
    removes its own staging folder either way). A format with no available
    extractor — `.rar` and `.7z` without 7-Zip, anything else at all — is a
    NoExtractorError naming the format and what would read it.

    A nested archive is extracted as a file like any other: a zip holding
    another zip leaves that zip in the tree, and nothing descends into it. The
    import that follows treats it as an unknown file (it is neither audio nor a
    sidecar), so a rip wrapped twice imports nothing until the inner archive is
    unpacked by hand — said plainly rather than silently unwrapped, which would
    be an unbounded second extraction of untrusted bytes.
    """
    label = os.path.basename(archive_path)
    kind = import_kind(archive_path)
    dest_dir = os.path.abspath(dest_dir)
    if kind is None:
        suffix = os.path.splitext(archive_path)[1].lower()
        raise NoExtractorError(
            f"{label}: {suffix or 'this file'} is not an archive this app can "
            f"unpack — it reads {SUPPORTED_ARCHIVES}")
    os.makedirs(dest_dir, exist_ok=True)

    if kind == "zip":
        try:
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    why = member_problem(info.filename)
                    if why:
                        raise _refuse(label, info.filename, why)
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise _refuse(label, info.filename, "symbolic link")
                    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or \
                            stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
                        raise _refuse(label, info.filename, "device node")
                zf.extractall(dest_dir)
        except zipfile.BadZipFile:
            # A compression method zipfile cannot read (or an encrypted zip):
            # 7-Zip reads both, and its listing is validated first.
            sevenz = _sevenzip_for("sevenzip", label)
            _check_sevenzip(sevenz, archive_path, label)
            extract_with_7z(sevenz, archive_path, dest_dir)
    elif kind == "tar":
        with tarfile.open(archive_path) as tf:
            for m in tf.getmembers():
                why = member_problem(m.name)
                if why:
                    raise _refuse(label, m.name, why)
                if m.issym() or m.islnk():
                    raise _refuse(label, m.name, "link")
                if not (m.isfile() or m.isdir()):
                    raise _refuse(label, m.name, "not a plain file or folder")
            log(f"{label}: extracting…")
            extract_tar(archive_path, dest_dir)
    else:                                   # .7z / .rar
        sevenz = _sevenzip_for(kind, label)
        _check_sevenzip(sevenz, archive_path, label)
        log(f"{label}: extracting with 7-Zip…")
        extract_with_7z(sevenz, archive_path, dest_dir)

    _verify_tree(dest_dir, label)
