"""Managed slskd (Soulseek) integration.

slskd (https://github.com/slskd/slskd) is a single-binary Soulseek client
with a REST API. It is vendored into .dependencies like every other tool;
this module:

  * generates slskd's YAML config from MLO settings (credentials, profile
    description, listen/web ports, upload/download limits, shares = the
    library folder <music folder>/Artists, download dir),
  * spawns/monitors the process,
  * exposes a thin REST client (search, downloads, transfers, profile).

The generated config lives at <music folder>/.mlo/data/slskd.yaml; a random
web API key is minted per start and kept in memory (the web UI is bound to
localhost).
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import traceback
import uuid
from urllib.parse import quote

import httpx

from mlo.config import load_config
from mlo.fetchdeps import installed_path
from mlo.paths import LIB_AUDIO_EXTS, library_root
from server.beetscfg import REPO_ROOT

_proc_lock = threading.RLock()  # reentrant: start() holds it while calling is_running()
_proc = {"proc": None, "api_key": None, "started_at": 0.0}

CREATE_NO_WINDOW = 0x08000000


def config_path():
    """slskd.yaml lives in <music folder>/.mlo/data with the rest of the state."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "slskd.yaml")


# --------------------------------------------------------------------------- #
# Binary + config
# --------------------------------------------------------------------------- #
def slskd_exe():
    d = installed_path("slskd")
    if d:
        for name in ("slskd.exe", "slskd"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def slskd_installed():
    return slskd_exe() is not None


def _yq(text):
    # slskd (a .NET app) needs NATIVE separators in paths: a forward-slash
    # share root gets concatenated with backslashes internally ("F:/x\y"),
    # which fails the share scan with "Only absolute paths may be specified"
    # and silently shares no files. Normalize before quoting.
    text = str(text)
    if os.name == "nt":
        text = text.replace("/", "\\")
    return '"' + text.replace("\\", "\\\\").replace('"', "'") + '"'


def download_dir(cfg=None):
    """Where slskd saves files (<music folder>/.mlo/downloads by default)."""
    cfg = cfg or load_config()
    d = str(cfg.get("soulseek_download_dir") or "").strip()
    if d:
        return d
    music = str(cfg.get("music_folder") or "").strip()
    from mlo.paths import app_data_dir, downloads_dir
    if music:
        return downloads_dir(music)
    return os.path.join(app_data_dir(), "downloads")


_RESERVED_SHARE_FILTERS = [
    # regex filters applied on top of every share config — the app's own
    # state/transient folders must never be shared to the network. '.mlo'
    # covers the whole hidden root (data/downloads/trash); '.mlo_data' is
    # kept for an install that has not migrated yet.
    "'\\.mlo'",
    "'\\.mlo_data'",
    "'\\.mlo_trash'",
    "'\\.mlo_downloads'",
    "'\\.data'",
    "'(^|[\\\\/])Data([\\\\/]|$)'",
    "'(^|[\\\\/])\\.data([\\\\/]|$)'",
    # Junk a library folder picked up on a NAS or a network share: Synology's
    # metadata dir and recycle bin, Finder/Explorer droppings, and the
    # AppleDouble sidecars a Mac leaves beside every file. slskd's own example
    # config excludes the same three file names — nobody wants to browse them,
    # and they inflate every share listing with entries that are not music.
    "'(^|[\\\\/])@eaDir([\\\\/]|$)'",
    "'(^|[\\\\/])#recycle([\\\\/]|$)'",
    "'(^|[\\\\/])\\.DS_Store$'",
    "'(^|[\\\\/])Thumbs\\.db$'",
    "'(^|[\\\\/])desktop\\.ini$'",
    "'(^|[\\\\/])\\._[^\\\\/]*$'",
]


def share_dirs(cfg=None):
    """Folders shared to the Soulseek network (default: the library,
    `<music folder>/Artists`).

    The default used to be the music folder itself, which published everything
    the app keeps BESIDE the library: `.mlo` (data, downloads, in-flight
    partials, trash) and every folder that was dropped into the music folder
    and never filed by the organizer. The reserved filters covered that by
    accident — one edited filter entry and the network browses the app's own
    state. The library root is what "share my library" means: an explicit
    `soulseek_share_dirs` still wins, and a blank/missing music folder still
    shares nothing (the audit reports that as unconfigured)."""
    cfg = cfg or load_config()
    dirs = [str(d).strip() for d in (cfg.get("soulseek_share_dirs") or []) if str(d).strip()]
    if not dirs:
        music = str(cfg.get("music_folder") or "").strip()
        # Only ever asked with the folder THIS cfg names: library_root() falls
        # back to the config file on disk when it is given nothing, and a share
        # list must describe the config it was handed.
        root = library_root(music) if music else None
        if root:
            dirs = [root]
    return dirs


def _int_setting(cfg, key, default=0):
    """One numeric setting, or `default` when it is missing or not a number.

    The settings blob arrives as JSON from a browser, so a string can land
    where a number belongs; `int()` on it would raise out of generate_yaml and
    take the whole slskd start (and every share) with it."""
    try:
        return int(str(cfg.get(key)).strip() or 0)
    except (TypeError, ValueError):
        return default


def _is_absolute_share_path(path):
    """True when slskd accepts the path as a share root.

    slskd validates every entry of `shares.directories` at boot and exits on a
    relative one ("only absolute paths are supported"), so a bad entry is not
    one missing share — it takes the whole daemon, and with it search, browse
    and downloads."""
    p = str(path or "")
    if os.name == "nt":
        return bool(re.match(r"^(\\\\[^\\/]|[a-zA-Z]:[\\/])", p))
    return p.startswith("/")


def _share_entries(cfg=None):
    """The share list as slskd must receive it: [(path, alias)], plus what was
    left out as [(raw, reason)].

    Two entries that normalize to the same folder, or two folders with the same
    leaf name, make slskd refuse to start ("alias the same path" / "collide") —
    and the settings field for extra folders is free text, so both are one typo
    away. A folder that is not absolute kills it the same way. Duplicates and
    unusable entries are dropped here (reported by share_audit) and a colliding
    leaf gets an explicit `[alias]`, which is the only form slskd can tell
    apart. The alias is the leaf name otherwise, so the remote path a share has
    today does not change."""
    entries, dropped, seen = [], [], {}
    for raw in share_dirs(cfg):
        path = os.path.normpath(os.path.expanduser(str(raw).strip()))
        if not _is_absolute_share_path(path):
            dropped.append((raw, "not an absolute path"))
            continue
        key = os.path.normcase(path)
        if key in seen:
            dropped.append((raw, f"already configured as {seen[key]}"))
            continue
        seen[key] = path
        entries.append(path)
    leaves = {}
    for path in entries:
        leaves.setdefault(_share_leaf(path), 0)
        leaves[_share_leaf(path)] += 1
    used, out = set(), []
    for path in entries:
        leaf = _share_leaf(path)
        alias = leaf
        if leaves.get(leaf, 0) > 1:
            i = 2
            while alias in used:
                alias = f"{leaf}-{i}"
                i += 1
        used.add(alias)
        out.append((path, alias))
    return out, dropped


def _share_leaf(path):
    """slskd's own alias for a share with no `[alias]`: the last path segment
    (Share(string share) -> Alias = share.Split('/','\\').Last())."""
    text = str(path or "").rstrip("\\/")
    return text.split("\\")[-1].split("/")[-1] or text


def _share_entry_yaml(path, alias):
    """One `shares.directories` entry: the bare path, or `[alias]path` when the
    leaf name would collide with another share."""
    if alias and alias != _share_leaf(path):
        return _yq(f"[{alias}]{path}")
    return _yq(path)


def invalid_share_filters(cfg=None):
    """User exclude patterns slskd would reject: [(pattern, reason)].

    slskd compiles every `shares.filters` entry as a .NET regex at boot and
    exits when one does not compile — the same "no daemon, no share" failure as
    a bad path. They are left out of the generated config and reported instead
    of being written."""
    cfg = cfg or load_config()
    out = []
    for raw in (cfg.get("soulseek_share_exclude") or []):
        text = str(raw).strip()
        if not text:
            continue
        try:
            re.compile(text)
        except re.error as e:
            out.append((text, str(e)))
    return out


def _inside(path, folder):
    """True when `path` is strictly inside `folder` (boundary-aware).

    Same test server/main.py applies to its music-folder guard: a plain
    string-prefix would accept a sibling whose name merely starts with the
    folder's ("C:\\Music2" is not inside "C:\\Music")."""
    try:
        p = os.path.abspath(os.path.normpath(path))
        f = os.path.abspath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if p == f:
        return False
    # paths on different drives have no common ancestor at all
    if os.path.normcase(os.path.splitdrive(p)[0]) != os.path.normcase(os.path.splitdrive(f)[0]):
        return False
    try:
        return os.path.normcase(os.path.commonpath([p, f])) == os.path.normcase(f)
    except (OSError, ValueError, TypeError):
        return False


def _download_exclude(cfg):
    """slskd filter that keeps the download dir out of the share index.

    Sharing recurses into subdirectories, so a user-set download dir inside
    the music folder published in-progress transfers — and the names of what
    is being downloaded — to strangers. Quoted/boundary-aware like the
    reserved filters (slskd compiles them as .NET regexes against native
    paths, hence the [\\\\/] separators)."""
    ddir = download_dir(cfg)
    # the default dir (<music>/.mlo/downloads) is already covered by the
    # reserved filters; only a user-set one needs a pattern of its own
    if not str(cfg.get("soulseek_download_dir") or "").strip() or not ddir:
        return None
    if not any(_inside(ddir, d) for d in share_dirs(cfg)):
        return None
    # escape only the regex metacharacters (.NET accepts "\ " too, but the
    # yaml is easier to read without it)
    name = "".join(("\\" + c) if c in "\\.^$|?*+()[]{}" else c
                   for c in os.path.basename(os.path.normpath(ddir)))
    return f"'(^|[\\\\/]){name}([\\\\/]|$)'"


def share_exclude(cfg=None):
    """Extra share exclude regexes from settings, on top of the reserved ones.

    A pattern slskd cannot compile is left out (see invalid_share_filters):
    writing it would stop the daemon from booting at all."""
    cfg = cfg or load_config()
    bad = {p for p, _why in invalid_share_filters(cfg)}
    extra = [str(x).strip() for x in (cfg.get("soulseek_share_exclude") or []) if str(x).strip()]
    out = _RESERVED_SHARE_FILTERS + [f"'{x}'" for x in extra if x not in bad]
    d = _download_exclude(cfg)
    if d:
        out.append(d)
    return out


# slskd's destination template for every batch this app enqueues, written to
# `transfers.download.destination.subdirectory`. The tokens are slskd 0.26's
# own (DownloadService.DeriveDestination): ${SOURCE_USERNAME}, ${SOURCE_PATH},
# ${SOURCE_DIRECTORY}, ${BATCH_ID}, ${BATCH_EXTERNAL_ID}, ${SEARCH_ID},
# ${SEARCH_TEXT} — slskd has NO artist/album variable.
#
# WHY not slskd's default `${SOURCE_DIRECTORY}` (the remote folder's LEAF
# only): one enqueue call is one batch, so `${BATCH_ID}` gives every candidate
# a root of its own — two peers' copies of the same album can never share a
# folder and drag a stranger's files into an import. `${SOURCE_PATH}` keeps the
# peer's own album/disc structure below that root, which is what the importer
# needs: the leaf-only default flattened a peer's `Album/CD1` + `Album/CD2`
# into two TOP-LEVEL `CD1/` + `CD2/` folders whose common parent is the
# download root itself, so _local_album_root() refused every multi-disc album.
# Result: `<downloads>/<user>/<batch id>/<remote path>/<file>` — below the
# download dir, one candidate per folder.
DESTINATION_SUBDIR = "${SOURCE_USERNAME}/${BATCH_ID}/${SOURCE_PATH}"


def generate_yaml(cfg=None):
    """Render slskd config; returns (yaml_text, api_key)."""
    cfg = cfg or load_config()
    api_key = uuid.uuid4().hex
    username = str(cfg.get("soulseek_username") or "").strip()
    password = str(cfg.get("soulseek_password") or "").strip()
    description = str(cfg.get("soulseek_description") or "").strip()
    listen_port = int(cfg.get("soulseek_listen_port") or 50000)
    web_port = int(cfg.get("soulseek_web_port") or 5030)
    # Fallback 9 = the shipped default (soulseek_search_concurrency ×
    # soulseek_candidate_slots, see server.soulseek_auto): the pipeline's own
    # two ceilings are what the app enforces, and this is the slot count slskd
    # needs to serve the product they promise.
    dl_slots = max(1, min(20, _int_setting(cfg, "soulseek_download_slots", 9)))
    # slskd validates `transfers.upload.slots` as Range(1, int.MaxValue) and
    # EXITS when a value falls outside it (Program.TryValidate), so the "0 =
    # unlimited" the settings field documents was a config slskd refused to
    # boot with: no daemon, no share, no search. slskd has no unlimited slot
    # count — 0/blank now means slskd's own default of 10 simultaneous
    # uploads, which is the behaviour the setting was asking for.
    ul_slots = max(1, min(20, _int_setting(cfg, "soulseek_upload_slots", 0) or 10))
    # Speed limits, in KiB/s (0 = unlimited, emitted as slskd's int.MaxValue
    # default). The Settings UI's "kB/s" fields write soulseek_up_limit /
    # soulseek_down_limit, which the old YAML never read at all — so a limit
    # typed there was silently ignored. They are the fallback now (kB/s and
    # KiB/s differ by 2.4%, immaterial for a throttle).
    up_kib = max(0, int(cfg.get("soulseek_upload_limit_kib") or 0)
                 or int(cfg.get("soulseek_up_limit") or 0) * 1000)
    down_kib = max(0, int(cfg.get("soulseek_download_limit_kib") or 0)
                   or int(cfg.get("soulseek_down_limit") or 0) * 1000)
    downloads = download_dir(cfg)
    # slskd's own share list: deduped, absolute, with an explicit alias where a
    # leaf name would collide — anything else makes slskd refuse to start.
    shares, _dropped_shares = _share_entries(cfg)
    exclude = share_exclude(cfg)

    lines = [
        "# Generated by la musica — edits are overwritten.",
        "web:",
        f"  port: {web_port}",
        # slskd's HTTPS listener otherwise binds a second port (5031) with a
        # generated cert — an extra listener the app never uses (it talks
        # plain HTTP to the loopback port) and never reports.
        "  https:",
        f"    disabled: {'false' if cfg.get('soulseek_web_https') else 'true'}",
        "  authentication:",
        "    disabled: true",
        "    api_keys:",
        "      mlo:",
        f"        key: {api_key}",
        "        role: readwrite",
    ]
    # The Soulseek network account is written ONCE, under `soulseek:` below —
    # that is the only place slskd reads it (docs/config.md documents
    # `authentication` solely under `web:` and `metrics:`; a top-level block
    # was never part of the schema). A second, top-level `authentication:`
    # block used to be written here as well, which nothing read and which
    # invited the reader to think the pair was configured there.
    # Transfer slots + speed limits live under `transfers` in slskd; the old
    # `soulseek.global_upload_limit` / `global_download_limit` keys do not
    # exist in slskd 0.26 and were silently ignored (the limits never
    # applied). speed_limit is KIBIBYTES/second and slskd rejects 0
    # (Range(1,..)), so "unlimited" means omitting the key — slskd's own
    # default is int.MaxValue.
    transfers = ["transfers:", "  upload:", f"    slots: {ul_slots}"]
    if up_kib > 0:
        transfers.append(f"    speed_limit: {up_kib}")
    transfers += ["  download:", f"    slots: {dl_slots}"]
    if down_kib > 0:
        transfers.append(f"    speed_limit: {down_kib}")
    # slskd's own key is transfers -> download -> destination -> subdirectory
    # (Options.TransfersOptions.DownloadOptions.DestinationOptions.Subdirectory).
    # Anywhere else it is silently ignored and every download keeps slskd's
    # flat `${SOURCE_DIRECTORY}` layout — see DESTINATION_SUBDIR for why that
    # layout is unusable here.
    transfers += [
        "    destination:",
        f"      subdirectory: {_yq(DESTINATION_SUBDIR)}",
    ]

    lines += [
        "soulseek:",
        f"  username: {_yq(username)}",
        f"  password: {_yq(password)}",
        f"  description: {_yq(description)}",
        "  listen_ip_address: 0.0.0.0",
        f"  listen_port: {listen_port}",
    ] + transfers + [
        # slskd's option is `directories` (Options.DirectoriesOptions) with
        # `downloads` / `incomplete` inside it. Written as `dirs:` slskd
        # silently ignored the section and saved everything to its own
        # default (%LOCALAPPDATA%\slskd\downloads) — which made every import
        # wait forever for files that never appeared in the app's dir.
        #
        # `incomplete` is a SIBLING of `downloads`, not a child: partial
        # files must not sit inside the folder the app lists, imports from
        # and shares.
        "directories:",
        f"  downloads: {_yq(downloads)}",
        f"  incomplete: {_yq(_incomplete_dir(cfg))}",
    ]
    if shares and cfg.get("soulseek_share_library", True):
        lines += ["shares:", "  directories:"]
        lines += [f"    - {_share_entry_yaml(p, alias)}" for p, alias in shares]
        lines.append("  filters:")
        lines += [f"    - {x}" for x in exclude]
    text = "\n".join(lines) + "\n"
    return text, api_key


def _incomplete_dir(cfg=None):
    """slskd's staging dir — the SIBLING `incomplete` of the download dir.

    Always derived from the download dir's parent, so ONE rule covers both
    layouts: the default `<music>/.mlo/downloads` yields
    `<music>/.mlo/incomplete`, and a custom `soulseek_download_dir` on another
    volume keeps both halves of a transfer on the SAME filesystem — slskd's
    move from incomplete to complete is then a rename, not a full copy."""
    from mlo.paths import INCOMPLETE_DIR_NAME
    downloads = download_dir(cfg)
    parent = os.path.dirname(os.path.abspath(downloads)) or downloads
    return os.path.join(parent, INCOMPLETE_DIR_NAME)


def _ensure_dirs(cfg=None):
    """Create slskd's download + incomplete folders; returns the download dir.

    slskd validates both with DirectoryExists(ensureWriteable: true) and
    refuses to boot otherwise — it exited instantly and the app only ever saw
    "slskd did not become ready in time".

    Old installs staged partials in `<downloads>/.incomplete`. That tree is
    renamed to the new sibling FIRST, before anything creates the target: a
    rename only (never a copy of a half-written file), and only when the target
    does not already exist, so it is safe on every boot and does nothing once
    done."""
    downloads = download_dir(cfg)
    incomplete = _incomplete_dir(cfg)
    # Migration must precede the makedirs below — creating `incomplete` first
    # would make the "target does not exist" test always false and the legacy
    # tree would never move.
    legacy = os.path.join(downloads, ".incomplete")
    if os.path.isdir(legacy) and not os.path.exists(incomplete):
        try:
            os.replace(legacy, incomplete)
        except OSError:
            # Locked by a running slskd: leave it and try again next boot. The
            # new dir gets created below either way, so slskd uses the right
            # one and the old tree is just stale until the rename succeeds.
            pass
    for d in (downloads, incomplete):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return downloads


def write_config(cfg=None):
    text, api_key = generate_yaml(cfg)
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _ensure_dirs(cfg)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return api_key


# --------------------------------------------------------------------------- #
# Process management
# --------------------------------------------------------------------------- #
def is_running():
    with _proc_lock:
        proc = _proc["proc"]
        if proc is None:
            return False
        if proc.poll() is not None:
            _proc["proc"] = None
            return False
        return True


def _web_status(cfg=None):
    """Response of GET /application on the configured slskd port (None when
    nothing answers there)."""
    try:
        client = _http_client(cfg)
        return client.get("/application", headers={"Accept": "application/json"},
                          timeout=1.0)
    except Exception:
        return None

def web_up(cfg=None):
    """True when an slskd web API answers openly on our port."""
    r = _web_status(cfg)
    return r is not None and r.status_code == 200

def _options_username(cfg=None):
    """The Soulseek username the answering slskd is configured with.

    slskd 0.26 dropped `user.username` from GET /application (it now carries
    only privileges/statistics), which left the foreign-instance check below
    blind to a squatter that simply runs with web auth disabled. GET /options
    still reports it and needs no credentials."""
    try:
        client = _http_client(cfg)
        r = client.get("/options", headers={"Accept": "application/json"},
                       timeout=1.0)
        if r.status_code != 200:
            return ""
        return str(((r.json() or {}).get("soulseek") or {}).get("username") or "").strip()
    except Exception:
        return ""


def instance_owner(cfg=None):
    """Who is answering on the slskd web port? -> (ours, username, why).

    Adopting a foreign instance made the UI report "running, not logged in"
    forever: its API either needs authentication or serves another Soulseek
    account. `why` carries a human explanation whenever it is not ours.
    """
    cfg = cfg or load_config()
    p = int(cfg.get("soulseek_web_port") or 5030)
    want = str(cfg.get("soulseek_username") or "").strip()
    r = _web_status(cfg)
    if r is None:
        return False, None, ""  # nothing listening on our port
    if r.status_code in (401, 403):
        return False, None, (f"another application's slskd is already using "
                             f"port {p} (it requires authentication)")
    if r.status_code != 200:
        return False, None, f"port {p} is used by another program (HTTP {r.status_code})"
    try:
        got = str(((r.json() or {}).get("user") or {}).get("username") or "").strip()
    except Exception:
        got = ""
    if not got:
        # slskd 0.26 /application no longer names the account; ask /options
        got = _options_username(cfg)
    if got and (not want or got.lower() != want.lower()):
        # A slskd that reports an account we do not expect is NOT ours: with
        # no saved username (an empty `want`) the old `if want and got ...`
        # test called any answering instance ours, so start() adopted — and
        # stop() taskkilled — another app's slskd.
        who = f"signed in as {got}" if got else "with no account configured"
        return False, got, (f"another application's slskd is already using "
                            f"port {p} ({who})")
    return True, (got or None), ""


def _last_log_line(path, limit=220):
    """Last non-empty line of a log file ("" when unreadable/empty)."""
    try:
        with open(path, "rb") as f:
            lines = [l.strip() for l in
                     f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        return lines[-1][-limit:] if lines else ""
    except OSError:
        return ""


# slskd's console format is "[HH:MM:SS LVL] message"; the level is not the
# first bracketed token, so match the rendered " ERR]" rather than "[ERR]".
_LOG_LEVELS = (" ERR]", " WRN]", " FTL]")
_LOGIN_KEYS = ("login", "password", "credential", "soulseek server")


def login_error(cfg=None):
    """slskd's own reason for a failed Soulseek login ("" when it said nothing).

    The daemon owns the explanation — INVALIDPASS, an empty credential pair, a
    port it could not bind — and none of it reaches the REST API, so the last
    error/warning line of its (per-start truncated) log is republished
    verbatim. The app must never invent a reason the daemon did not give."""
    return _log_reason(_LOGIN_KEYS)


def _options_dirs(cfg=None):
    """slskd's live download dirs from GET /options, or {} when unreadable."""
    return (_live_options(cfg, timeout=1.0).get("directories") or {})


def _uses_our_downloads(cfg):
    """True when the answering slskd saves downloads where this app expects.

    An ADOPTED slskd (spawned by an earlier run, or by another launcher with
    this config file) keeps whatever directories it booted with, so after a
    download-dir change the app would wait forever on a folder slskd never
    writes to. Unreadable options mean "cannot tell" -> adopt as before."""
    got = str((_options_dirs(cfg) or {}).get("downloads") or "").strip()
    if not got:
        return True
    try:
        return (os.path.normcase(os.path.normpath(got))
                == os.path.normcase(os.path.normpath(download_dir(cfg))))
    except (OSError, ValueError):
        return True


def start(cfg=None):
    """Spawn slskd; returns (started, message). Idempotent.

    If an slskd from a previous backend run is still answering on the web
    port (orphaned child), it is adopted instead of spawning a duplicate —
    unless it is saving downloads somewhere else, in which case it is
    restarted so it reads the config this app just wrote. Paths are compared
    case-insensitively because this is Windows."""
    with _proc_lock:
        if is_running():
            return True, "already running"
        exe = slskd_exe()
        if not exe:
            return False, "slskd is not installed"
        # slskd 0.26's single-file zip ships no wwwroot; it refuses to boot
        # without it even though only the REST API is needed here.
        try:
            os.makedirs(os.path.join(os.path.dirname(exe), "wwwroot"), exist_ok=True)
        except OSError:
            pass
        api_key = write_config(cfg)
        # Adopt an already-listening slskd, but ONLY our own. A foreign one
        # (another app's slskd on the same port) must not be adopted: its
        # API rejects us and Stop would kill a process we do not own.
        ours, _who, why = instance_owner(cfg)
        if ours:
            if not _uses_our_downloads(cfg):
                stop(cfg)  # boots ours below, with the directories we wrote
            else:
                _proc["api_key"] = None
                _proc["started_at"] = time.time()
                _ensure_portmap_watcher()
                _portmap_soon(cfg, "adopted slskd")
                return True, "adopted already-running slskd"
        if why:
            return False, f"cannot start slskd: {why} — stop the other slskd " \
                           f"first (only one slskd can run at a time)"
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        log = os.path.join(os.path.dirname(config_path()), "slskd.log")
        try:
            logf = open(log, "wb")
        except OSError:
            logf = subprocess.DEVNULL
        proc = subprocess.Popen(
            [exe, "--config", config_path(), "--no-logo"],
            cwd=os.path.dirname(exe),
            stdout=logf,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        if hasattr(logf, "close"):
            logf.close()  # the child keeps its own handle
        # slskd allows ONE instance per machine. When another slskd is
        # already running, this one exits at once and the UI used to look
        # like a dead "Start" button for ever. Report what it actually said.
        deadline = time.time() + 2.0
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is not None:
            detail = _last_log_line(log)
            raise_msg = (f"slskd exited immediately ({detail}) — only one slskd "
                         f"can run at a time, so stop the other app's slskd first"
                         if detail else
                         "slskd exited immediately — another slskd is probably "
                         "already running on this machine")
            return False, raise_msg
        _proc["proc"] = proc
        _proc["api_key"] = api_key
        _proc["started_at"] = time.time()
        # The listen port is forwarded from here on: in the BACKGROUND, because
        # a router that answers slowly (or not at all) must never delay — let
        # alone fail — the client's start, and the same reconciler keeps the
        # mapping right when the port or the setting changes later.
        _ensure_portmap_watcher()
        _portmap_soon(cfg, "slskd start")
        return True, "started"


def _is_slskd_pid(pid):
    """True when the PID's image is slskd (Windows tasklist lookup).

    Something else squatting slskd's web port must never be taskkilled just
    because it happens to answer on that port."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return "slskd" in (out or "").lower()


def _kill_port_listener(port):
    """Hard-kill whatever process listens on the slskd web port.

    Needed for ADOPTED slskd instances (spawned by a previous backend run):
    no child handle exists, so a plain terminate is impossible and the
    process would otherwise keep running after the user presses Stop.
    Only slskd images are killed — the caller has already established that
    the listener is ours (instance_owner)."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            pids = set()
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" and f":{port} " in ln:
                    pids.add(parts[-1])
            killed = False
            for pid in pids:
                if not pid.isdigit() or pid == "0":
                    continue
                if not _is_slskd_pid(pid):
                    continue
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=10)
                killed = True
            return killed
        r = subprocess.run(["fuser", "-k", f"{port}/tcp"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def stop(cfg=None):
    cfg = cfg or load_config()
    port = int(cfg.get("soulseek_web_port") or 5030)
    # Whoever answers on our port is only ours to kill when instance_owner()
    # says so: another app's slskd (web auth required, or signed in as a
    # different account) must be left running. Stop never kills a process we
    # do not own.
    with _proc_lock:
        proc = _proc["proc"]
        _proc["proc"] = None
    adopted = proc is None or proc.poll() is not None
    if adopted:
        # untracked (adopted) slskd — the only way down is via the port
        stopped = _kill_port_listener(port) if instance_owner(cfg)[0] else False
    else:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except OSError:
            pass
        stopped = True
        # a straggler or adopted sibling can still hold the web port
        time.sleep(0.3)
        if instance_owner(cfg)[0]:
            _kill_port_listener(port)
    # The pooling client holds sockets to a dead slskd (and its API key). This
    # runs on the adopted path too — it used to `return` early there, leaving
    # a client pointing at the old base URL/API key for the next start.
    _close_client()
    return stopped


# --------------------------------------------------------------------------- #
# The listen port: is it forwarded, and is it even usable here?
# --------------------------------------------------------------------------- #
# slskd has no UPnP/NAT-PMP of its own (upstream closed the request
# unimplemented), so the mapping is this app's to make — and to be honest about.
# A Soulseek client whose listen port is closed looks OFFLINE to the network:
# peers cannot initiate the download connection, and downloads stall even
# though search and login work. The mapping is therefore made when the client
# starts, replaced when the listen port changes, and removed when the setting is
# turned off — never claimed when a router did not confirm it.
#
# Two facts are kept apart on purpose: what the ROUTER was told (mlo.portmap's
# structured result, in _PORTMAP below) and who holds the port on THIS machine
# (listen_port_state), because they fail for different reasons and only the
# second one is visible from inside.
_PORTMAP_LOCK = threading.Lock()
_PORTMAP = {
    "result": None,      # the last structured result from mlo.portmap
    "port": 0,           # the port that result is about
    "checked_at": 0.0,   # when it was obtained
    "in_flight": False,  # an attempt is running right now
    "reason": "",        # why the last attempt was made
}
# How often the watcher re-reads the config. A saved setting never goes through
# this module, so a changed listen port (or the toggle being turned off) is only
# visible by reading the config again — a small JSON read, with no network call
# unless something actually differs.
_PORTMAP_POLL = 20.0
_PORTMAP_WATCH = {"thread": None, "stop": threading.Event()}
# The router may cap a mapping's lease. Re-asking at half the granted lifetime
# keeps a lease we were told about from expiring unnoticed.
_PORTMAP_REFRESH_AT = 0.5


def _portmap_store(result=None, port=0, reason="", in_flight=False):
    """Record what the router was told (and about which port)."""
    with _PORTMAP_LOCK:
        if result is not None:
            _PORTMAP["result"] = result
            _PORTMAP["port"] = int(port or 0)
            _PORTMAP["checked_at"] = time.time()
        if reason:
            _PORTMAP["reason"] = reason
        _PORTMAP["in_flight"] = bool(in_flight)


def _portmap_cfg(cfg=None):
    """(enabled, listen_port) from the config, defensively read."""
    cfg = cfg or load_config()
    return (bool(cfg.get("soulseek_upnp", True)),
            _int_setting(cfg, "soulseek_listen_port", 50000))


def _portmap_expired(result, checked_at):
    """True when the gateway-granted lease of `result` has run out.

    `checked_at` is when the app ASKED (the result itself carries only the
    expiry the gateway stated), so the lease length is the difference between
    the two."""
    expires = float((result or {}).get("expires_at") or 0.0)
    if not expires:
        return False  # no lease was stated: nothing to expire
    lifetime = max(0.0, expires - float(checked_at or 0.0))
    return time.time() >= expires - lifetime * (1.0 - _PORTMAP_REFRESH_AT)


def _portmap_release(port, reason):
    """Ask the router to drop a mapping this app made (best effort)."""
    from mlo import portmap
    try:
        return portmap.close_port(port, timeout=3.0)
    except Exception as e:  # never let a router take the client down with it
        traceback.print_exc()
        return {"state": "error", "ok": False, "method": "", "listen_port": port,
                "detail": f"the mapping of port {port} could not be removed "
                          f"({e})", "tried": [], "attempts": [], "verified": False,
                "external_ip": "", "internal_ip": "", "gateway": "",
                "expires_at": 0.0}


def portmap_sync(cfg=None, reason="", force=False):
    """Make the router's mapping match the config. Never raises.

    `enabled` off removes a mapping this app made; on, it ensures the current
    listen port is forwarded, replacing the previous port's mapping if it
    changed (a forward to a port nothing listens on any more is worse than
    none). Returns the state `portmap_state` reports."""
    cfg = cfg or load_config()
    enabled, port = _portmap_cfg(cfg)
    with _PORTMAP_LOCK:
        previous = dict(_PORTMAP["result"] or {})
        previous_port = int(_PORTMAP["port"] or 0)
        asked_at = float(_PORTMAP["checked_at"] or 0.0)
        _PORTMAP["in_flight"] = True
        _PORTMAP["reason"] = reason or _PORTMAP["reason"]
    try:
        if not enabled:
            if previous_port and previous.get("state") == "mapped":
                _portmap_store(_portmap_release(previous_port, reason), 0, reason)
            else:
                # Nothing of ours to remove: say the FEATURE is off, and do not
                # invent a router answer for a request that was never sent.
                _portmap_store({"state": "off", "ok": False, "method": "",
                                "listen_port": port,
                                "detail": "automatic port opening is switched "
                                          "off in settings",
                                "tried": [], "attempts": [], "verified": False,
                                "external_ip": "", "internal_ip": "", "gateway": "",
                                "expires_at": 0.0}, 0, reason)
            return portmap_state(cfg)
        if previous_port and previous.get("state") == "mapped" and previous_port != port:
            _portmap_release(previous_port, reason)
        if (not force and previous.get("state") == "mapped"
                and previous_port == port
                and not _portmap_expired(previous, asked_at)):
            _portmap_store(None, 0, reason)
            return portmap_state(cfg)
        if not client_running(cfg):
            # A forward with nothing listening behind it is not what the user
            # asked for, so nothing is claimed: the client is simply not up.
            _portmap_store(None, 0, reason)
            return portmap_state(cfg)
        from mlo import portmap
        result = portmap.open_port(port)
        _portmap_store(result, port, reason)
        if not result.get("ok"):
            # The router's own words (or the network's silence) go to the app
            # log: a refused mapping is exactly the kind of thing that is
            # invisible until someone wonders why nobody downloads from them.
            print(f"[mlo] port mapping: {result.get('detail')}")
        return portmap_state(cfg)
    finally:
        _portmap_store(None, 0, "", in_flight=False)


def client_running(cfg=None):
    """True when an slskd of ours answers (spawned by us, or adopted)."""
    return is_running() or web_up(cfg)


def portmap_state(cfg=None):
    """What is known about the LISTEN port's router mapping.

    `state` is `mlo.portmap`'s own verdict (`mapped`/`refused`/`no_gateway`/
    `unsupported`/`error`), or one of this app's own: `off` (the setting is
    off), `pending` (never attempted since this process started), `checking`
    (an attempt is running now), `client_down` (nothing to map for). Nothing
    here is guessed: a mapping is only `mapped` when a router confirmed it."""
    cfg = cfg or load_config()
    enabled, port = _portmap_cfg(cfg)
    with _PORTMAP_LOCK:
        result = dict(_PORTMAP["result"] or {})
        checked_at = float(_PORTMAP["checked_at"] or 0.0)
        in_flight = bool(_PORTMAP["in_flight"])
        attempted_port = int(_PORTMAP["port"] or 0)
        reason = str(_PORTMAP["reason"] or "")
    out = {"enabled": enabled, "listen_port": port, "checked_at": checked_at,
           "in_flight": in_flight, "reason": reason, "state": "", "detail": "",
           "method": "", "verified": False, "external_ip": "", "internal_ip": "",
           "gateway": "", "tried": [], "attempts": [], "mapped_port": attempted_port,
           "expires_at": 0.0}
    if not enabled:
        out["state"] = "off"
        out["detail"] = ("Automatic port opening is off — the listen port has to "
                         "be forwarded on the router by hand.")
        return out
    state = str(result.get("state") or "")
    # `expires_at` from the gateway's own answer: a lease it granted and has not
    # had re-asked since may have run out, which is the one thing about a
    # confirmed mapping that can go stale on its own.
    for key in ("detail", "method", "verified", "external_ip", "internal_ip",
                "gateway", "tried", "attempts", "expires_at"):
        out[key] = result.get(key) or out[key]
    if state == "released":
        # A mapping this app made was removed and the switch is still on: no
        # mapping of THIS port is in place yet (the watcher's next pass asks
        # for one). Reporting that as "off" would claim the feature is
        # disabled, and as "mapped" would claim a forward nobody confirmed.
        out["state"] = "pending"
        out["detail"] = (f"{out['detail']} A mapping of port {port} has not been "
                         f"made yet.").strip()
        return out
    if state in ("mapped", "refused", "no_gateway", "unsupported", "error"):
        out["state"] = state
        return out
    if in_flight:
        out["state"] = "checking"
        out["detail"] = "asking the router for the listen port mapping"
        return out
    if not client_running(cfg):
        out["state"] = "client_down"
        out["detail"] = ("slskd is not running, so the port is not mapped yet — "
                         "the mapping is made when it starts.")
        return out
    out["state"] = "pending"
    out["detail"] = ("the router has not been asked for a mapping yet in this "
                     "run of the app.")
    return out


def _portmap_watch():
    """Re-reconcile the mapping when the listen port or the setting changes."""
    while not _PORTMAP_WATCH["stop"].is_set():
        try:
            portmap_sync(load_config(), reason="watch")
        except Exception:
            traceback.print_exc()
        _PORTMAP_WATCH["stop"].wait(_PORTMAP_POLL)


def _ensure_portmap_watcher():
    """Start the reconciler once, on the first client start of this process."""
    with _PORTMAP_LOCK:
        thread = _PORTMAP_WATCH["thread"]
        if thread is not None and thread.is_alive():
            return
        _PORTMAP_WATCH["stop"].clear()
        _PORTMAP_WATCH["thread"] = threading.Thread(target=_portmap_watch,
                                                    daemon=True,
                                                    name="mlo-portmap")
        _PORTMAP_WATCH["thread"].start()


def _portmap_soon(cfg, reason="slskd start"):
    """Ask the router in the BACKGROUND: a slow or absent router must never hold
    up the client's start (and slskd starts listening regardless)."""
    def run():
        try:
            portmap_sync(cfg, reason=reason, force=True)
        except Exception:
            traceback.print_exc()
    threading.Thread(target=run, daemon=True, name="mlo-portmap-once").start()


def listen_port_state(cfg=None):
    """The LISTEN port's real state here: who holds it, and what the router
    was told.

    Bindability is probed FIRST because it is the cheap and decisive test: a
    port this process can bind is a port nothing holds, so nothing is listening
    on it either. On Windows a connect to a closed port does not even get a
    refusal — it sits out the whole timeout — while a bind answers instantly.
    When the bind fails, a connect tells the two remaining cases apart: a
    listener ACCEPTS (that is what peers need, and with an slskd of ours running
    it is the daemon [INFERENCE — the app cannot see whose socket it is]; with
    no slskd running it is another program, which is exactly the conflict the
    web port already reports), and a port that is held without accepting (a
    socket left in TIME_WAIT by a previous run, or another bound socket) fails
    the connect."""
    cfg = cfg or load_config()
    port = _int_setting(cfg, "soulseek_listen_port", 50000)
    ours = client_running(cfg)
    out = {"listen_port": port, "listening": False, "holder": "",
           "bindable": None, "conflict": None, "error": "",
           "mapping": portmap_state(cfg)}
    free, why = _port_bindable(port)
    out["bindable"] = free
    if free:
        if ours:
            # slskd runs and the port is unheld: nothing is listening there, and
            # the daemon's own log line (listen_port_error) is the only place
            # that failure is ever explained — this app does not invent a cause.
            out["error"] = (f"slskd is running but nothing is listening on the "
                            f"Soulseek listen port {port}")
        return out
    if _port_accepts(port):
        out["listening"] = True
        out["holder"] = "slskd" if ours else "another program"
        if not ours:
            out["conflict"] = (f"another program is listening on the Soulseek "
                               f"listen port {port} — slskd cannot use it while "
                               f"that program runs")
    else:
        out["error"] = (f"port {port} cannot be bound on this machine right now "
                        f"({why}) — slskd will fail to listen on it until "
                        f"whatever holds the port is released")
    return out


def _port_bindable(port):
    """Can this machine bind the LISTEN port? -> (free, reason it is not)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("", port))
        return True, ""
    except OSError as e:
        return False, f"{e.strerror or e}"
    finally:
        sock.close()


def _port_accepts(port):
    """Does something LISTEN on the port (a TCP connect is accepted)?"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def port_status_payload(cfg=None):
    """The LISTEN port's state for the status route / share audit: what this
    machine does with the port (and who holds it) plus what the router was told.

    `slskd_error` is the daemon's OWN line about a port it could not use — the
    only place that failure is explained, since slskd reports nothing about the
    listener over REST."""
    cfg = cfg or load_config()
    state = listen_port_state(cfg)
    state["slskd_error"] = listen_port_error()
    return state


# slskd's own words for a listen port it could not use. Matched narrowly —
# ERR/WRN level AND one of these words — because republishing an unrelated line
# as the reason would be worse than saying nothing.
_PORT_ERROR_KEYS = ("listen", "bind", "address already in use", "socket address")


def listen_port_error():
    """slskd's own last word about a listen port ("" when it said nothing)."""
    return _log_reason(_PORT_ERROR_KEYS)


def _log_reason(keys):
    """The last ERR/WRN line of slskd's log mentioning any of *keys*."""
    log = os.path.join(os.path.dirname(config_path()), "slskd.log")
    try:
        with open(log, "rb") as f:
            lines = [l.strip() for l in
                     f.read().decode("utf-8", "replace").splitlines() if l.strip()]
    except OSError:
        return ""
    for line in reversed(lines):
        if any(lvl in line for lvl in _LOG_LEVELS) and \
                any(k in line.lower() for k in keys):
            return line.split("] ", 1)[-1].strip()[:300]
    return ""


# --------------------------------------------------------------------------- #
# REST client (slskd /api/v0)
# --------------------------------------------------------------------------- #
def _base_url(cfg=None):
    cfg = cfg or load_config()
    web_port = int(cfg.get("soulseek_web_port") or 5030)
    return f"http://127.0.0.1:{web_port}/api/v0"


def _headers():
    key = _proc["api_key"]
    h = {"Accept": "application/json"}
    if key:
        h["X-API-Key"] = key
    return h


# ONE keep-alive client for every slskd REST call. The auto-importer polls
# the transfer tree every 1-3 s; building a client per request meant a fresh
# TCP handshake (and a fresh connection pool) on every tick. The client is
# rebuilt when the base URL or the API key changes — slskd mints a new key on
# every boot, so a restart gets a new client — and closed by stop().
_client_lock = threading.Lock()
_client = {"client": None, "base": None, "key": None}


def _close_client_locked():
    client = _client["client"]
    _client.update(client=None, base=None, key=None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _close_client():
    """Drop the pooled connections (slskd stopped, port/key changed)."""
    with _client_lock:
        _close_client_locked()


def _http_client(cfg=None):
    base = _base_url(cfg)
    key = _proc["api_key"]
    with _client_lock:
        client = _client["client"]
        if client is None or _client["base"] != base or _client["key"] != key:
            _close_client_locked()
            client = httpx.Client(base_url=base, timeout=30.0)
            _client.update(client=client, base=base, key=key)
        return client


def _error_text(response):
    """slskd's own message for a failed request, as plain text.

    slskd puts the reason in the body — a peer that went offline between the
    search and the enqueue answers 500 `User <name> appears to be offline`,
    and the download-request limiter answers 429. ASP.NET may wrap it in a
    ProblemDetails object; both shapes are read here, and the body is read
    from the bytes httpx already buffered (never re-fetched)."""
    text = ""
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        text = str(body.get("message") or body.get("detail") or body.get("title") or "")
    if not text:
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""
    if not text:
        return ""
    # An HTML error page (a proxy in the way, a crashed ASP.NET pipeline) names
    # no reason we can use, and must not land in a job log or a toast.
    if text.lstrip()[:1] == "<":
        return ""
    return " ".join(text.split())[:200]


class SlskdHTTPError(httpx.HTTPStatusError):
    """An slskd refusal that carries slskd's own reason in `str(e)`.

    httpx's message is only "Server error '500 Internal Server Error' for url
    …", which hides the actionable half of every slskd failure — a peer that
    went offline between the search and the enqueue answers 500 with the text
    `User <name> appears to be offline`. Subclassing (rather than replacing)
    keeps every existing `except httpx.HTTPStatusError` handler and its
    `e.response.status_code` checks working untouched.
    """

    def __init__(self, message, *, request, response, detail=""):
        super().__init__(message, request=request, response=response)
        self.detail = (detail or "").strip()

    def __str__(self):
        base = super().__str__()
        return f"{base} — slskd said: {self.detail}" if self.detail else base


class SlskdError(RuntimeError):
    """An slskd refusal with no HTTP status to attach (a refused enqueue).

    Same contract as `SlskdHTTPError` for the callers that report a reason:
    `str(e)` is what the user reads.
    """

    def __init__(self, message, path=""):
        self.message = (message or "").strip()
        super().__init__(f"{self.message}{f' ({path})' if path else ''}")


def _request(method, path, json_body=None, timeout=30.0, retries=1):
    client = _http_client()
    for attempt in range(retries + 1):
        r = client.request(method, path, json=json_body, headers=_headers(),
                           timeout=timeout)
        if r.status_code in (401, 403) and _proc["api_key"]:
            # stale key from a previous slskd boot — the running instance
            # mints its own; retry anonymously (auth is localhost-only)
            _proc["api_key"] = None
            r = client.request(method, path, json=json_body, headers=_headers(),
                               timeout=timeout)
        # 429 is slskd's download-request limiter (a global two-slot
        # semaphore): the request was NOT accepted, so it is safe to re-send
        # once after a moment instead of failing the caller's whole album.
        if r.status_code == 429 and attempt < retries:
            time.sleep(1.5)
            continue
        # 3xx too: the pooled client does not follow redirects, and
        # raise_for_status() — which this replaced — treated a redirect as an
        # error. Nothing here wants a redirect body parsed as a payload.
        if r.status_code >= 300:
            raise SlskdHTTPError(
                f"slskd {method} {path} answered {r.status_code} for url "
                f"'{r.request.url}'",
                request=r.request, response=r, detail=_error_text(r))
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


def wait_until_ready(timeout=25.0):
    """Poll the slskd application info endpoint until it answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _request("GET", "/application", timeout=2.0)
            return True
        except Exception:
            time.sleep(0.25)
    return False


# --------------------------------------------------------------------------- #
# High-level operations
# --------------------------------------------------------------------------- #
def search(query, cfg=None, timeout_ms=None, response_limit=None):
    """Start a search; returns the search id for polling.

    slskd validates a client-supplied id as a GUID — older builds ignored
    it, current ones answer 400 to anything else (which broke every search).
    A real GUID is sent; if still refused, retry without one and use
    whatever id slskd assigned.

    timeout_ms extends the search duration per request. The unit is
    MILLISECONDS, measured from the last peer response: slskd's own DTO
    comment claims seconds, but the value reaches Soulseek.NET's
    SearchOptions as ms — measured against slskd 0.26, `searchTimeout: 45`
    ends the search in ~1.5 s with 0 responses while `5000` runs ~27 s and
    collects 62. Sending a seconds-style number therefore kills the search
    outright (this is what starved candidate discovery in auto-import).

    response_limit is slskd's `responseLimit`: the search terminates with
    state `ResponseLimitReached` once that many peers have answered. The
    quiet timer alone is not enough for a popular album — peers keep replying,
    so the search never goes quiet and nothing is readable until it does
    (slskd only serves `/searches/{id}/responses` once a search has ENDED).
    Measured: `responseLimit: 5` on an active query returns 5 responses and
    76 files after ~1.4 s instead of the full window."""
    search_id = str(uuid.uuid4())
    body = {"id": search_id, "searchText": query}
    if timeout_ms:
        # slskd's DTO field is SearchTimeout; a "timeout" key is ignored.
        body["searchTimeout"] = max(1, int(timeout_ms))
    if response_limit:
        body["responseLimit"] = max(1, int(response_limit))
    try:
        resp = _request("POST", "/searches", json_body=body, timeout=30.0)
    except httpx.HTTPStatusError as e:
        if e.response is None or e.response.status_code != 400:
            raise
        body.pop("id", None)
        resp = _request("POST", "/searches", json_body=body, timeout=30.0)
    remote = str((resp or {}).get("id") or "").strip()
    return remote or search_id


_SEARCH_TERMINAL_STATES = ("Completed", "TimedOut", "ResponseLimitReached",
                           "Cancelled", "Errored", "FileLimitReached")
# terminal AND failed: that search will never hand back a downloadable file
_SEARCH_ERROR_STATES = ("Errored",)


def is_search_done(res_or_state):
    """True when an slskd search has reached a terminal state.

    slskd states are bitwise flags that serialize to comma-separated strings
    like 'Completed, TimedOut' or 'InProgress'. Errored and FileLimitReached
    are terminal too — polling them until the caller's deadline burned a
    whole search window per query.
    """
    if isinstance(res_or_state, dict):
        if res_or_state.get("isComplete"):
            return True
        st = res_or_state.get("state")
    else:
        st = res_or_state
    if not st or st == "InProgress":
        return False
    return any(x in str(st) for x in _SEARCH_TERMINAL_STATES)


def search_error(res_or_state):
    """The terminal state that made a search FAIL ("" when it did not).

    slskd reports an errored search as a finished one with no responses;
    callers must not read that as "no candidate folders found"."""
    st = res_or_state.get("state") if isinstance(res_or_state, dict) else res_or_state
    st = str(st or "")
    return next((x for x in _SEARCH_ERROR_STATES if x in st), "")


def search_results(search_id, cfg=None):
    """Aggregate file responses for a finished/in-progress search."""
    try:
        state = _request("GET", f"/searches/{search_id}")
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return {
                "state": "NotFound",
                "isComplete": True,
                "responseCount": 0,
                "fileCount": 0,
                "responses": [],
            }
        raise
    responses = []
    try:
        resp_list = _request("GET", f"/searches/{search_id}/responses") or []
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            resp_list = []
        else:
            raise
    for r in resp_list or []:
        username = r.get("username") or ""
        slot = bool(r.get("hasFreeUploadSlot"))
        speed = int(r.get("uploadSpeed") or 0)
        queue = int(r.get("queueLength") or 0)
        for f in r.get("files") or []:
            name = f.get("filename") or ""
            responses.append({
                "username": username,
                "file": name,
                "size": int(f.get("size") or 0),
                "bitrate": f.get("bitrate"),
                # slskd's response files carry `length` (seconds) — not
                # `duration` — plus the codec facts the UI classifies by.
                "duration": f.get("length"),
                "vbr": f.get("vbr"),
                "ext": (f.get("extension") or os.path.splitext(name)[1].lstrip(".")).lower(),
                "bit_depth": f.get("bitDepth"),
                "sample_rate": f.get("sampleRate"),
                "slot": slot,
                "speed": speed,
                "queue": queue,
            })
    st_dict = state if isinstance(state, dict) else {}
    # The counters describe the list ABOVE them, and they are computed from it
    # rather than read off the search's state: slskd's own `responseCount` /
    # `fileCount` only settle once a search has ENDED, so a page that renders
    # them beside a file list which comes from `/responses` shows "found 0 files
    # from 0 peers" over a list of hits (the reported metrics bug), and a search
    # stopped early by a response limit never settles at all. One response is
    # one peer, one entry in `responses` is one file — the same two numbers the
    # UI is about to draw. The state's counters are the fallback for the window
    # where slskd has counted responses it will not serve yet.
    peers = {r["username"] for r in responses if r["username"]}
    return {
        "state": st_dict.get("state"),
        "isComplete": bool(st_dict.get("isComplete")),
        "responseCount": len(peers) if responses else int(st_dict.get("responseCount") or 0),
        "fileCount": len(responses) if responses else int(st_dict.get("fileCount") or 0),
        "responses": responses,
    }


def search_many(queries, cfg=None, timeout_ms=None, response_limit=None):
    """Start ONE slskd search per query, ALL AT ONCE. Returns (ids, errors).

    slskd has no notion of a multi-query search, and the manual search by MBID
    asks several questions about ONE track (its artist + title, its album, its
    own MusicBrainz id), so each question is its own search — the same "post
    them all, poll them in one loop" shape the auto-import batch uses, which is
    what makes the wall time one window instead of one per query. A query slskd
    refuses is reported beside the ids that DID start rather than dropped."""
    ids, errors = [], []
    for q in queries:
        try:
            ids.append(search(q, cfg, timeout_ms=timeout_ms,
                              response_limit=response_limit))
        except Exception as e:          # one bad query, not a failed search
            errors.append(f"{q}: {e}")
    return ids, errors


def search_ids(search_id):
    """The slskd ids ONE poll key names.

    A single search answers with its own id; the manual MBID search answers
    with the ids of every query it started, comma-joined, so one poll key stays
    one key (see `search_results_many`)."""
    return [part.strip() for part in str(search_id or "").split(",") if part.strip()]


def search_results_many(search_ids_):
    """The MERGED result of several searches, in `search_results`' own shape.

    The responses of every search in the list, deduped by (peer, file) — one
    peer is one peer however many queries saw it, the same rule the job's own
    multi-query poll applies — with the two counters recomputed from the list
    and `isComplete` true only once EVERY search has reached a terminal state.
    A search slskd no longer knows (the app restarted, it was cancelled) counts
    as one that answered nothing: it must not fail the whole poll."""
    merged, seen_any, complete, state = {}, False, True, None
    for sid in search_ids_:
        try:
            res = search_results(sid)
        except Exception:
            continue
        seen_any = True
        if state is None:
            state = res.get("state")
        if not res.get("isComplete"):
            complete = False
        for r in res.get("responses") or []:
            merged[(r.get("username") or "", r.get("file") or "")] = r
    responses = list(merged.values())
    return {
        "state": state,
        "isComplete": bool(seen_any and complete),
        "responseCount": len({u for u, _f in merged if u}),
        "fileCount": len(responses),
        "responses": responses,
    }


def cancel_search(search_id, cfg=None):
    """Cancel a running search (slskd DELETE /searches/{id}).

    A search otherwise runs to the end of its window: the caller is committed
    to the full search time plus the grace tail, and the UI's stop only stops
    polling. An already-finished search answers 404/400, which is not an
    error — the goal state (no search running) already holds."""
    if not search_id:
        return False
    try:
        _request("DELETE", f"/searches/{quote(str(search_id), safe='')}", timeout=15.0)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (400, 404):
            return False
        raise
    return True


def enqueue_download(username, files, cfg=None):
    """Queue files for download: files = [{filename, size}].

    slskd's enqueue route is per-user (POST /transfers/downloads/{username})
    with a bare list body — there is no top-level /downloads route.

    The 201 body reports what slskd queued and what the peer refused
    (`{Enqueued, Failed}`). A refusal is NOT a success: the transfer never
    starts, so the caller would wait out its whole timeout for files that were
    never asked for. Raises `SlskdError` naming the refused files instead.
    """
    body = [{"filename": f["filename"], "size": int(f.get("size") or 0)}
            for f in files]
    res = _request("POST", f"/transfers/downloads/{quote(str(username), safe='')}",
                   json_body=body, timeout=30.0)
    failed = res.get("Failed") if isinstance(res, dict) else None
    if failed:
        names = []
        for item in failed:
            name = item.get("filename") if isinstance(item, dict) else str(item)
            if name:
                names.append(str(name).replace("\\", "/").rsplit("/", 1)[-1])
        shown = ", ".join(names[:3])
        raise SlskdError(
            f"{len(failed)} file(s) refused by the peer"
            + (f": {shown}{'…' if len(names) > 3 else ''}" if shown else ""),
            "enqueue",
        )
    return True


def downloads_state(cfg=None):
    """Full download transfer tree, grouped per user."""
    try:
        return _request("GET", "/transfers/downloads") or []
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise


# Transfer states that mean the transfer is OVER and can be dropped from the
# tracked list. Matched as substrings because slskd reports compounds like
# "Completed, Succeeded". Anything not listed (Queued, Initializing,
# InProgress, Requested) is still live and must never be cleared.
_FINISHED_STATES = ("Succeeded", "Completed", "Errored", "Cancelled",
                    "Rejected", "FileNotFound", "Aborted", "TimedOut", "Failed")


def finished_transfer(state):
    """True when a slskd transfer state names a finished (removable) transfer."""
    st = str(state or "")
    return any(x in st for x in _FINISHED_STATES)


def successful_transfer(state):
    """True when a slskd transfer state names a SUCCEEDED download.

    Only "Succeeded" — slskd reports its failures as compounds too
    ("Completed, Errored", "Completed, Cancelled"), so "Completed" alone is a
    finished transfer, not a good one."""
    st = str(state or "")
    return finished_transfer(st) and "Succeeded" in st


def _duration_seconds(value):
    """slskd serializes TimeSpan fields as "hh:mm:ss" ("d.hh:mm:ss" past a
    day, e.g. a long queue wait); a bare number is already seconds. None when
    there is nothing readable — the caller shows "unknown" for that."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    days, _, rest = text.partition(".")
    if not days.isdigit():          # no day prefix: the whole value is h:mm:ss
        days, rest = "0", text
    secs = 0
    for part in rest.split(":"):
        if not part.isdigit():
            return None
        secs = secs * 60 + int(part)
    return secs + int(days) * 86400


def _percent(value, done, size):
    """percentComplete clamped to 0..100, falling back to bytes/size when
    slskd omits it (a transfer it has not started measuring yet).

    Kept to one decimal: slskd reports a float and the UI shows it — a
    900 MB file stepping in whole percent moves in ~9 MB jumps, which reads
    as a frozen bar."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        pct = (100.0 * done / size) if size else 0.0
    return max(0.0, min(100.0, round(pct, 1)))


def _user_transfers(slsk, username, pending, states=None):
    """slskd's transfers of `username` whose remote filename is in `pending`.

    [{"id", "filename", "state", "bytes", "size", "percent", "speed",
    "remaining"}], optionally narrowed to states containing one of `states`.
    `bytes`/`size`/`percent`/`speed`/`remaining` are the progress metrics the
    auto-importer's payload needs (contract §2): bytesTransferred, size,
    percentComplete, averageSpeed (bytes/s) and remainingTime in seconds
    (None when slskd does not report one). Best effort — an older API without
    a transfer tree, or one that is momentarily unreachable, simply yields no
    verdicts."""
    try:
        tree = slsk.downloads_state() or []
    except Exception:
        return []
    user = str(username or "").lower()
    out = []
    for entry in tree:
        if str(entry.get("username") or "").lower() != user:
            continue
        for d in entry.get("directories") or []:
            for f in d.get("files") or []:
                name = f.get("filename") or ""
                if name not in pending:
                    continue
                state = str(f.get("state") or "")
                if states and not any(x in state for x in states):
                    continue
                done = int(f.get("bytesTransferred") or 0)
                size = int(f.get("size") or 0)
                out.append({
                    "id": f.get("id"),
                    "filename": name,
                    "state": state,
                    "bytes": done,
                    "size": size,
                    "percent": _percent(f.get("percentComplete"), done, size),
                    "speed": float(f.get("averageSpeed") or 0.0),
                    "remaining": _duration_seconds(f.get("remainingTime")),
                })
    return out


def cancel_downloads(username, transfer_ids, cfg=None, failed=None):
    """Cancel + untrack downloads of one user (DELETE /transfers/downloads/…).

    slskd's cancel route is per transfer and takes an optional ?remove=true
    ("also drop it from the tracked list"). A rejected/errored transfer stays
    queued otherwise, so slskd re-requests the very files whose partials the
    auto-importer just deleted. Best effort: a transfer that is already gone
    answers 404/400 and must not abort the run.

    When `failed` (a list) is passed, the ids slskd did NOT confirm dropped are
    appended to it — only a real refusal or an unreachable slskd, never the
    404/400 of a transfer that was already gone."""
    user = quote(str(username), safe="")
    for tid in transfer_ids:
        if not tid:
            continue
        try:
            _request("DELETE",
                     f"/transfers/downloads/{user}/{quote(str(tid), safe='')}?remove=true",
                     timeout=15.0)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else 0
            if failed is not None and code not in (400, 404):
                failed.append(str(tid))
            continue
        except Exception:
            if failed is not None:
                failed.append(str(tid))
            continue
    return True


def uploads_state(cfg=None, timeout=30.0):
    """Full upload transfer tree (what others have downloaded = shared
    history), grouped per user. Same shape as downloads_state()."""
    try:
        return _request("GET", "/transfers/uploads", timeout=timeout) or []
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise


def upload_start_frames(prev, uploads):
    """Which users just STARTED downloading from us, and the state to keep.

    `uploads` is slskd's upload tree (uploads_state()); `prev` is the `state`
    this function answered with last time: {username: active count}. A user is
    active while ANY of their transfers is not in a finished state — Queued,
    Initializing, InProgress and Requested all mean slskd still owes the peer
    a file, and a transfer slskd reports as finished must never announce
    anything at first sight (it may have been taken hours before this watcher
    ever ran).

    Returns (state, frames): the state for the next pass, and one
    {"username", "files"} per user whose count went from nothing to something
    — ONE frame per user-session, not one per poll (the watcher runs every
    few seconds and a transfer takes minutes). A user whose transfers all
    finished is dropped from the state, so their next download re-arms the
    frame.

    Pure — slskd, the clock and the event bus stay outside — which is what
    makes the dedupe itself testable with plain dicts."""
    prev = prev or {}
    state, frames = {}, []
    for entry in uploads or []:
        if not isinstance(entry, dict):
            continue
        user = str(entry.get("username") or "")
        if not user:
            continue
        active = 0
        for d in entry.get("directories") or []:
            for f in (d or {}).get("files") or []:
                if not finished_transfer((f or {}).get("state")):
                    active += 1
        if active:
            state[user] = active
    for user, count in state.items():
        if prev.get(user):
            continue      # already sharing: the frame went out when it started
        frames.append({"username": user, "files": count})
    return state, frames


def server_state(cfg=None):
    """Login status + server stats (or None when logged out)."""
    try:
        return _request("GET", "/server")
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (401, 403, 404, 503):
            return None
        raise


def user_info(username, cfg=None):
    """Remote user profile (speeds, slots, files shared)."""
    return _request("GET", f"/users/{username}/info", timeout=15.0)


# A peer's browse is a whole share tree — thousands of directories, tens of
# megabytes (one live peer: 8655 dirs / 22.6 MB). Memoizing briefly means
# opening the browse view twice, or a retry, does not re-pull it.
_BROWSE_TTL = 120.0
_browse_cache: dict = {}
_browse_lock = threading.Lock()


def _normalize_browse(payload):
    """slskd's browse payload -> [{directory, files:[{filename, size, ext, length}]}].

    slskd 0.26 answers with an OBJECT ({"directories": [{"name", "fileCount",
    "files": [...]}], "directoryCount": N}) where each file's `filename` is a
    bare NAME — the remote path a download needs is directory + separator +
    name. Older builds returned the list already keyed by `directory`. Both
    shapes are normalized here so callers only ever see full remote paths.
    """
    if isinstance(payload, dict):
        raw = payload.get("directories") or []
    else:
        raw = payload or []
    out = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        directory = str(d.get("name") or d.get("directory") or "")
        files = []
        for f in d.get("files") or []:
            if not isinstance(f, dict):
                continue
            name = str(f.get("filename") or "")
            if not name:
                continue
            if directory and "\\" not in name and "/" not in name:
                name = directory.rstrip("\\/") + "\\" + name
            files.append({
                "filename": name,
                "size": int(f.get("size") or 0),
                "ext": str(f.get("extension") or "").lstrip(".").lower(),
                "length": f.get("length"),
            })
        out.append({"directory": directory, "files": files})
    return out


def browse(username, cfg=None, use_cache=True):
    """Every shared directory of a remote user: [{directory, files:[…]}]."""
    key = str(username or "").strip().lower()
    now = time.time()
    if use_cache:
        with _browse_lock:
            hit = _browse_cache.get(key)
        if hit and now - hit[0] < _BROWSE_TTL:
            return hit[1]
    rows = _normalize_browse(
        _request("GET", f"/users/{quote(str(username), safe='')}/browse", timeout=120.0))
    if use_cache and key:
        with _browse_lock:
            _browse_cache[key] = (now, rows)
            # a handful of peers is all anyone opens at once; keep it bounded
            if len(_browse_cache) > 8:
                oldest = min(_browse_cache.items(), key=lambda kv: kv[1][0])[0]
                _browse_cache.pop(oldest, None)
    return rows


# --------------------------------------------------------------------------- #
# Private messages (slskd's /api/v0/conversations routes)
# --------------------------------------------------------------------------- #
def _message_user(username):
    """A username as a path segment. Soulseek usernames contain spaces and
    slskd's route parameter is [UrlEncoded], so the segment has to be
    percent-encoded before it goes into the path."""
    return quote(str(username or ""), safe="")


def _request_status(method, path, json_body=None, timeout=30.0):
    """_request()'s twin for the one route whose MEANING is the status code:
    slskd answers a send with 201 when it went out and 200 when the peer is
    blacklisted/ignored — with no body either way, so _request() cannot tell
    the two apart. Same pooled client, same header auth and same stale-key
    retry as _request()."""
    client = _http_client()
    r = client.request(method, path, json=json_body, headers=_headers(),
                       timeout=timeout)
    if r.status_code in (401, 403) and _proc["api_key"]:
        _proc["api_key"] = None
        r = client.request(method, path, json=json_body, headers=_headers(),
                           timeout=timeout)
    r.raise_for_status()
    return r.status_code


def _acknowledge(path):
    """PUT an acknowledge route: True when slskd took it, False when there is
    no such conversation/message (404). Anything else propagates."""
    try:
        _request("PUT", path, timeout=15.0)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return False
        raise
    return True


def conversations(cfg=None, include_inactive=False, unacknowledged_only=False):
    """Every conversation (GET /conversations); [] when there are none.

    slskd lists active conversations only unless include_inactive is set. The
    entries carry no last-message or timestamp — a preview line would cost one
    request per conversation — so a list shows the peer and its unread count
    (unAcknowledgedMessageCount) instead."""
    query = (f"?includeInactive={str(bool(include_inactive)).lower()}"
             f"&unAcknowledgedOnly={str(bool(unacknowledged_only)).lower()}")
    return _request("GET", f"/conversations{query}", timeout=15.0) or []


def conversation(username, cfg=None, include_messages=True):
    """One conversation (GET /conversations/{username}), or None when slskd
    has no conversation with that user (404)."""
    user = _message_user(username)
    inc = str(bool(include_messages)).lower()
    try:
        return _request("GET", f"/conversations/{user}?includeMessages={inc}",
                        timeout=15.0)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def messages(username, cfg=None, unacknowledged_only=False):
    """A conversation's messages, oldest-first as slskd keeps them
    (GET /conversations/{username}/messages).

    None — not [] — when there is no such conversation (404), so a caller can
    answer "unknown peer" instead of showing an empty thread."""
    user = _message_user(username)
    unack = str(bool(unacknowledged_only)).lower()
    try:
        return _request("GET",
                        f"/conversations/{user}/messages?unAcknowledgedOnly={unack}",
                        timeout=15.0) or []
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def send_message(username, message, cfg=None):
    """Send a private message (POST /conversations/{username}); returns
    slskd's status code.

    The body is a BARE JSON string ("hello"), not an object — slskd binds it
    as [FromBody] string. 201 means slskd handed the message to the network,
    200 means the user is blacklisted/ignored and it went nowhere; the caller
    decides what to tell the user. An empty message answers 400 and raises
    like any other upstream error."""
    return _request_status("POST", f"/conversations/{_message_user(username)}",
                           json_body=message, timeout=15.0)


def acknowledge_message(username, message_id, cfg=None):
    """Mark one message read (PUT /conversations/{username}/{id}); False when
    slskd has no such conversation or message (404)."""
    user = _message_user(username)
    return _acknowledge(f"/conversations/{user}/{quote(str(message_id), safe='')}")


def acknowledge_conversation(username, cfg=None):
    """Mark every message of a conversation read (PUT /conversations/{username});
    False when there is no such conversation (404)."""
    return _acknowledge(f"/conversations/{_message_user(username)}")


def close_conversation(username, cfg=None):
    """Close a conversation (DELETE /conversations/{username}); False when
    there is no such conversation (404). slskd answers 204, which the shared
    client hands back as None."""
    try:
        _request("DELETE", f"/conversations/{_message_user(username)}",
                 timeout=15.0)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return False
        raise
    return True


def restart(cfg=None):
    """Restart slskd so it re-reads its config and re-scans shares.

    slskd indexes shares at boot; new/removed/renamed files in the library
    are only visible to other users after a restart (older slskd builds
    have no share-rescan API). Returns True when slskd answers again.
    """
    stop()
    ok, _msg = start(cfg)
    if not ok:
        return False
    return wait_until_ready(timeout=40.0)


def application_info(cfg=None):
    return _request("GET", "/application", timeout=5.0)


def shares_state(cfg=None):
    """slskd's live share configuration + scan state (None when down)."""
    try:
        return _request("GET", "/shares", timeout=10.0)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (401, 403, 404, 503):
            return None
        raise


def rescan_shares(cfg=None):
    """Ask slskd to rescan its share index (PUT /shares).

    slskd answers 409 while a scan is already running; that is not a failure
    of this call but it is also not a scan this call started, so it is
    reported as such instead of being swallowed."""
    try:
        _request("PUT", "/shares", timeout=30.0)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 409:
            raise SlskdError("slskd is already scanning its shares") from e
        raise
    return True


# --------------------------------------------------------------------------- #
# Share audit: is this library actually searchable and browsable by others?
# --------------------------------------------------------------------------- #
# Almost every way a Soulseek share "works" while nobody can see it is silent
# in slskd: an unreadable subdirectory is skipped (IgnoreInaccessible), a share
# path that does not exist is logged and skipped, filters prune whole subtrees,
# and the index is only rebuilt when the daemon scans. In all of those cases
# slskd reports a *successful* scan of fewer — or no — files.
#
# What the network can see is slskd's own share state, so the audit reads that
# (GET /application -> shares for the scan state, /shares for the live share
# list, /options for what the running daemon was started with, /server for the
# login) and compares it with the disk and with the config this app generates.
# Nothing is inferred from intent: a daemon that does not answer, a config it
# never loaded, an empty index and an index that is missing a file that is on
# disk are each reported as their own state.

# Worst first: the audit's status is the first of these that applies.
_AUDIT_STATUS_RANK = (
    "not_running", "not_logged_in", "unconfigured", "path_unreadable",
    "config_mismatch", "scan_failed", "not_scanned", "scanning",
    "empty_share", "unbrowsable", "listen_unconfirmed", "misconfigured", "ok",
    "disabled",
)
_AUDIT_RANK = {name: i for i, name in enumerate(_AUDIT_STATUS_RANK)}

# The directory names the reserved filters exclude — the disk walk prunes the
# same ones so its file count is comparable with slskd's index (which always
# skips hidden/system entries).
_JUNK_DIR_NAMES = {".mlo", ".mlo_data", ".mlo_trash", ".mlo_downloads",
                   ".data", "Data", "@eaDir", "#recycle", ".AppleDouble"}

# The object slskd's share index is pulled with. A large library's index is
# tens of megabytes and this runs from a button press, so the read is capped
# and a capped read reports "could not verify" instead of a verdict it did not
# observe.
_PROBE_MAX_BYTES = 24 * 1024 * 1024
# The disk walk that says how much there is to share: bounded, and memoized so
# a tab that keeps refetching the share state does not re-walk the library.
_DISK_WALK_MAX = 60000
_DISK_DIR_MAX = 20000
_DISK_WALK_TTL = 120.0
_DISK_CACHE: dict = {}
_DISK_LOCK = threading.Lock()


def _live_options(cfg=None, timeout=5.0):
    """slskd's live options (GET /options), {} when unreadable.

    These are the options the RUNNING daemon was started with — the only way
    to tell "the config this app writes" from "the config slskd is using"."""
    try:
        client = _http_client(cfg)
        r = client.get("/options", headers={"Accept": "application/json"},
                       timeout=timeout)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}


def live_share_state(cfg=None):
    """slskd's live share state, or None when it cannot be read.

    slskd 0.26 publishes the scan state on GET /application (`shares`:
    scanPending/scanning/ready/faulted/cancelled/scanProgress/directories/
    files). `GET /shares` answers only the configured entries
    ({host: [{localPath, remotePath, alias, directories, files, isExcluded}]})
    and carries no scan state at all, which is why the UI reading a scan state
    out of it never showed one. Per-share directories/files stay null until a
    scan has updated the statistics."""
    try:
        info = _request("GET", "/application", timeout=5.0) or {}
        shares = _request("GET", "/shares", timeout=10.0) or {}
    except Exception:
        return None
    raw_scan = info.get("shares") if isinstance(info, dict) else None
    raw_scan = raw_scan if isinstance(raw_scan, dict) else {}
    scan = {
        "scanning": bool(raw_scan.get("scanning")),
        "pending": bool(raw_scan.get("scanPending")),
        "ready": bool(raw_scan.get("ready")),
        "faulted": bool(raw_scan.get("faulted")),
        "cancelled": bool(raw_scan.get("cancelled")),
        "progress": round(float(raw_scan.get("scanProgress") or 0.0) * 100.0, 1),
        "files": int(raw_scan.get("files") or 0),
        "directories": int(raw_scan.get("directories") or 0),
        "hosts": [str(h) for h in (raw_scan.get("hosts") or [])],
    }
    entries = []
    if isinstance(shares, dict):
        for host, rows in shares.items():
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                entries.append({
                    "host": str(host),
                    "local": str(row.get("localPath") or ""),
                    "remote": str(row.get("remotePath") or ""),
                    "alias": str(row.get("alias") or ""),
                    "files": int(row.get("files") or 0),
                    "directories": int(row.get("directories") or 0),
                    "excluded": bool(row.get("isExcluded")),
                })
    options = _live_options(cfg)
    return {"scan": scan, "shares": entries, "options": options}


def _share_log_lines(limit=6):
    """slskd's own last lines about scanning shares, newest last.

    slskd's REST API carries no scan timestamps or reasons, so the daemon's
    words are read from the log this app starts it with (the same source the
    login error comes from)."[HH:MM:SS LVL]" is its console format."""
    log = os.path.join(os.path.dirname(config_path()), "slskd.log")
    # slskd's own wording: "Starting shared file scan", "Scan found N files",
    # "Found N shared directories", "Failed to scan share ...", and the scan
    # error ("Encountered error during scan of shared files: ...").
    keys = ("shared file", "shared directories", "scan found",
            "failed to scan share", "share cache", "enumerating shared",
            "sharing ", "excluding ")
    try:
        with open(log, "rb") as f:
            lines = [l.strip() for l in
                     f.read().decode("utf-8", "replace").splitlines() if l.strip()]
    except OSError:
        return []
    out = [l for l in lines if any(k in l.lower() for k in keys)]
    return [l[-300:] for l in out[-limit:]]


def _walk_share_root(root, refresh=False):
    """(audio_files, newest_audio_path, newest_mtime, truncated) under a root.

    What the network *should* be able to see. Hidden entries and the same junk
    names slskd's filters exclude are pruned, so the count is comparable with
    the daemon's index. Bounded twice over (files and directories) so a share
    that holds a whole NAS does not stall the audit, and memoized briefly: this
    runs on every share-status fetch and the answer only feeds a comparison
    with slskd's own count. `refresh` forces the walk for the browse probe,
    which looks the file up by name — a file deleted in the last two minutes
    must not be reported as missing from the index.

    The newest file is what the probe asks slskd's index about — a file that
    certainly exists on disk right now."""
    key = os.path.normcase(os.path.abspath(root))
    if not refresh:
        with _DISK_LOCK:
            hit = _DISK_CACHE.get(key)
            if hit and time.time() - hit[0] < _DISK_WALK_TTL:
                return hit[1]
    count, visited, truncated = 0, 0, False
    newest = (0.0, "")
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        visited += 1
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in _JUNK_DIR_NAMES]
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(tuple(LIB_AUDIO_EXTS)):
                continue
            count += 1
            try:
                mtime = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            if mtime > newest[0]:
                newest = (mtime, os.path.join(dirpath, name))
        if count >= _DISK_WALK_MAX or visited >= _DISK_DIR_MAX:
            truncated = True
            break
    out = (count, newest[1], newest[0], truncated)
    with _DISK_LOCK:
        _DISK_CACHE[key] = (time.time(), out)
        if len(_DISK_CACHE) > 8:
            _DISK_CACHE.pop(min(_DISK_CACHE, key=lambda k: _DISK_CACHE[k][0]), None)
    return out


def _probe_file_rel(root, path):
    """A disk file's path below its share root, in slskd's own separator."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return ""
    if rel.startswith(".."):
        return ""
    return rel.replace("/", "\\").replace(os.sep, "\\")


def _index_has_file(dirs, root, path):
    """Is the disk file `path` in the index slskd serves? -> (found, detail).

    slskd names a directory by its REMOTE path (share alias + the folders below
    it) and each entry's file by its bare name, so the check matches the
    relative parent directory as a suffix — the alias cannot be assumed (it is
    the folder's leaf name, or an explicit one where two shares collide)."""
    rel = _probe_file_rel(root, path)
    if not rel:
        return False, f"{path} is outside the shared folder {root}"
    parent, _, name = rel.rpartition("\\")
    target = parent.lower()
    size = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        pass
    scanned = 0
    for row in dirs:
        scanned += len(row.get("files") or [])
        d = str(row.get("directory") or "").lower().replace("/", "\\")
        if target and not (d == target or d.endswith("\\" + target)):
            continue
        for f in row.get("files") or []:
            if str(f.get("filename") or "").split("\\")[-1].split("/")[-1].lower() != name.lower():
                continue
            got = int(f.get("size") or 0)
            if size and got and got != size:
                return False, (f"{name} is in the index but its size differs "
                               f"(index: {got} bytes, disk: {size} bytes)")
            return True, f"{rel} is in the index slskd serves"
    return False, (f"{rel} is not in the index slskd serves "
                   f"({len(dirs)} folders / {scanned} files were read)")


def _share_contents(cfg=None, limit_bytes=_PROBE_MAX_BYTES):
    """slskd's share index (GET /shares/contents) -> (dirs, truncated, error).

    This is the tree slskd answers a browse with, so it is the honest way to
    ask "can someone else see my files". [] with `error` set means it could not
    be read (slskd's or httpx's own words)."""
    client = _http_client(cfg)
    try:
        with client.stream("GET", "/shares/contents", headers=_headers(),
                           timeout=60.0) as r:
            if r.status_code >= 300:
                r.read()
                return [], False, (_error_text(r) or f"slskd answered {r.status_code}")
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > limit_bytes:
                    return [], True, ""
            body = b"".join(chunks)
    except Exception as e:
        return [], False, " ".join(str(e).split())[:200]
    if not body:
        return [], False, ""
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return [], False, "slskd's share index was not valid JSON"
    return _normalize_browse(payload), False, ""


def _served_uploads(cfg=None):
    """How many upload transfers slskd has handled, 0 when it cannot be read.

    A peer downloads from this share over a connection IT opens to the listen
    port, so any transfer in slskd's upload tree is proof that peers DO reach
    this port — the positive half of what a probe from inside the network can
    only describe. It is what keeps `listen_unconfirmed` from being permanent
    on an install whose forward was made by hand (a container can never see its
    router, and nothing else here can read the mapping)."""
    try:
        total = 0
        for entry in uploads_state(cfg, timeout=5.0) or []:
            if not isinstance(entry, dict):
                continue
            for d in entry.get("directories") or []:
                for f in (d or {}).get("files") or []:
                    if isinstance(f, dict) and f.get("state"):
                        total += 1
        return total
    except Exception:
        return 0


def _listen_hint(port_state):
    """What to do about an unconfirmed listen port, in THIS install's terms.

    A container cannot forward its own port: the gateway this process can see is
    Docker's bridge (172.18.x.1), so the automatic opening the switch asks for
    never reaches the home router, and the router can only forward to the
    HOST's address on the LAN. Saying "forward the port" without that is the
    advice that was already on screen while the share stayed unreachable."""
    port = int(port_state.get("listen_port") or 0)
    if port_state.get("container"):
        return (f"Running in a container: publish the port in docker-compose.yml "
                f"(ports: \"{port}:{port}\") and forward TCP {port} on the ROUTER "
                f"to the HOST's LAN address — a router cannot forward to a "
                f"container address, and automatic opening cannot reach the "
                f"router from in here (the gateway this process sees is Docker's "
                f"bridge). Then press Test port on this page.")
    return (f"Forward TCP {port} on the router to this machine's LAN address, or "
            f"turn on automatic port opening if the router speaks UPnP. Test "
            f"port on this page says what can be seen from here.")


def share_audit(cfg=None, probe=False):
    """What other Soulseek users can find, browse and download right now.

    `status` names one failure mode per distinct cause (no share configured, a
    folder slskd cannot read, a config the running daemon never loaded, a scan
    that failed, a scan that never finished, an index with no files, an index
    that does not hold a file that is on disk, a listen port no gateway
    confirmed a forward for) so the UI can say what is
    actually wrong instead of "not sharing". `probe=True` additionally pulls
    slskd's own share index and looks for a file that is on the disk.

    Every claim here is read back from slskd or from the filesystem; nothing is
    reported as working that was not observed working."""
    cfg = cfg or load_config()
    problems, notes, fired = [], [], set()
    # What the share's reachability problem IS, in the summary's own words (see
    # the listen-port block below). Empty until that block names one — and
    # `listen_unconfirmed` cannot be the status before it runs.
    listen_why = ""

    def add(status, code, message, hint=""):
        fired.add(status)
        for p in problems:
            if p["code"] == code and p["message"] == message:
                return
        problems.append({"code": code, "message": message, "hint": hint})

    def note(message):
        if message not in notes:
            notes.append(message)

    entries, dropped = _share_entries(cfg)
    bad_filters = invalid_share_filters(cfg)
    applied = [x.strip("'") for x in share_exclude(cfg)]
    audit = {
        "ok": False,
        "status": "ok",
        "summary": "",
        "problems": problems,
        "notes": notes,
        "shares": {
            "configured": [{"path": p, "alias": a} for p, a in entries],
            "live": [],
            "mismatch": False,
            "dropped": [{"path": p, "reason": r} for p, r in dropped],
        },
        "filters": {
            "applied": applied,
            "invalid": [{"pattern": p, "reason": r} for p, r in bad_filters],
            "mismatch": False,
        },
        "scan": {"state": "unknown", "scanning": False, "pending": False,
                 "ready": False, "faulted": False, "cancelled": False,
                 "progress": 0.0, "files": 0, "directories": 0, "log": []},
        "disk": {"roots": [], "audio_files": 0, "truncated": False,
                 "probe_file": ""},
        "browse": {"checked": False, "ok": None, "directories": 0, "detail": ""},
        # The LISTEN port as it really is here: whether anything accepts on it,
        # who holds it, and what the router was told (see listen_port_state).
        # `container` is filled in once the daemon has answered below.
        "port": dict(port_status_payload(cfg), container=False),
        "running": False,
    }

    if not bool(cfg.get("soulseek_share_library", True)):
        audit["status"] = "disabled"
        audit["summary"] = ("Sharing is switched off in settings — this library is "
                            "not offered to the Soulseek network.")
        audit["ok"] = True
        # ...unless the daemon is still serving the old list: the share is off in
        # the config, not in the process, and "switched off" must not be claimed
        # while strangers can still browse it.
        if entries and web_up(cfg):
            live = live_share_state(cfg)
            audit["running"] = live is not None
            if live and live["shares"]:
                audit["shares"]["live"] = live["shares"]
                audit["scan"].update(live["scan"])
                audit["status"] = "config_mismatch"
                audit["ok"] = False
                audit["summary"] = ("Sharing is switched off in settings, but the "
                                    "running slskd still serves "
                                    f"{len(live['shares'])} shared folder(s).")
                add("config_mismatch", "still_sharing",
                    "slskd is still sharing this library: its configuration is "
                    "read at boot.",
                    "Restart slskd (or save the share list) to stop sharing, or "
                    "turn the setting back on.")
        return audit

    def finish():
        status = min(fired, key=lambda s: _AUDIT_RANK.get(s, 0)) if fired else "ok"
        scan = audit["scan"]
        disk = audit["disk"]
        root = entries[0][0] if entries else ""
        if status == "ok":
            summary = (f"slskd is sharing {scan['files']} files in "
                       f"{scan['directories']} folders — other users can search, "
                       f"browse and download them.")
        elif status == "disabled":
            summary = "Sharing is switched off in settings."
        elif status == "not_running":
            summary = ("slskd is not running, so nothing is shared right now "
                       "(and searches fail too).")
        elif status == "not_logged_in":
            summary = ("slskd is not signed in to the Soulseek network — while it "
                       "is offline nobody can find or browse this share.")
        elif status == "unconfigured":
            summary = "No folder is shared, so the network cannot see this library."
        elif status == "path_unreadable":
            summary = "A shared folder cannot be read, so slskd shares nothing from it."
        elif status == "config_mismatch":
            summary = ("slskd is running with a share list this app did not write — "
                       "restart it to load the generated config.")
        elif status == "scan_failed":
            summary = "slskd's share scan failed, so its index is empty or stale."
        elif status == "not_scanned":
            summary = ("slskd has not finished indexing the shared folders yet — "
                       "other users find nothing until it does.")
        elif status == "scanning":
            summary = (f"slskd is indexing the shared folders "
                       f"({scan['progress']}% done) — the files it has reached "
                       f"are already visible.")
        elif status == "empty_share":
            audio = disk["audio_files"]
            summary = (f"slskd's index holds {scan['files']} files while {audio} "
                       f"audio file{'s' if audio != 1 else ''} sit"
                       f"{'s' if audio == 1 else ''} under {root} — other users "
                       f"can browse nothing.")
        elif status == "unbrowsable":
            summary = ("slskd's share index did not answer for a file that is on "
                       "disk — a browse of this share comes up short.")
        elif status == "listen_unconfirmed":
            summary = (f"slskd is sharing {scan['files']} files in "
                       f"{scan['directories']} folders — other users can find and "
                       f"search them, but {listen_why} the listen port, so a "
                       f"browse or a download FROM this client can fail until it "
                       f"is reachable.")
        elif status == "misconfigured":
            summary = ("slskd is sharing, but part of the share configuration was "
                       "left out of the generated config.")
        audit["status"] = status
        audit["ok"] = status == "ok"
        audit["summary"] = summary
        return audit

    for path, reason in dropped:
        add("misconfigured", "share_dropped",
            f"Share folder left out of slskd's config: {path} ({reason}).",
            "slskd validates its share list at boot and will not start on an "
            "entry it cannot use, so this one is dropped. Fix the path in the "
            "shared folders list.")
    for pattern, reason in bad_filters:
        add("misconfigured", "filter_invalid",
            f"Share exclude pattern is not a valid regular expression and was "
            f"left out: {pattern} ({reason}).",
            "slskd refuses to start on an invalid filter, so it is not written "
            "to its config. Remove or fix the pattern in settings.")

    if not entries:
        add("unconfigured", "no_share_dir", "No shared folder is configured.",
            "The library folder (<music folder>/Artists) is what gets shared by "
            "default, so set the music folder in Settings — or name the folders "
            "to share in the list above. Without one slskd shares nothing.")
        return finish()

    # ---- the disk: what is there to share at all -------------------------- #
    newest = (0.0, "", "")
    for path, _alias in entries:
        info = {"path": path, "exists": os.path.isdir(path), "readable": False,
                "audio_files": 0}
        if not info["exists"]:
            add("path_unreadable", "share_missing",
                f"{path} does not exist — slskd logs a warning and shares "
                f"nothing from there.",
                "Point the shared folder at the folder the music is actually in "
                "(the library defaults to <music folder>/Artists).")
        else:
            try:
                with os.scandir(path) as it:
                    next(it, None)
                info["readable"] = True
            except OSError as e:
                add("path_unreadable", "share_unreadable",
                    f"{path} cannot be read ({e.strerror or e}) — slskd shares "
                    f"nothing from it.",
                    "slskd runs as this app's user and needs READ access to the "
                    "whole folder; an unreadable subfolder is skipped silently.")
        if info["readable"]:
            try:
                count, newest_path, mtime, truncated = _walk_share_root(
                    path, refresh=probe)
            except OSError as e:
                count, newest_path, mtime, truncated = 0, "", 0.0, False
                add("path_unreadable", "share_unwalkable",
                    f"{path} could not be walked ({e}).")
            info["audio_files"] = count
            if truncated:
                audit["disk"]["truncated"] = True
            if mtime > newest[0]:
                newest = (mtime, newest_path, path)
        audit["disk"]["roots"].append(info)
    audit["disk"]["audio_files"] = sum(r["audio_files"] for r in audit["disk"]["roots"])
    audit["disk"]["probe_file"] = newest[1]

    # ---- is anything running to serve it? --------------------------------- #
    if not web_up(cfg):
        add("not_running", "slskd_down",
            "slskd is not answering on its web port, so nothing is shared.",
            "Press Start (or turn on Start with the app) — sharing lives in the "
            "daemon.")
        return finish()
    audit["running"] = True
    try:
        from server.auth import in_container
        audit["port"]["container"] = bool(in_container())
    except Exception:
        pass
    if audit["port"]["container"]:
        note("This app runs in a container: other users download from this share "
             "over the Soulseek listen port, which has to be published by "
             "docker-compose.yml (ports: \"<port>:<port>\") to the same port "
             "configured here.")
    # Every case below is a fact read off this machine or off the router —
    # nothing is inferred from intent, and a mapping nobody confirmed is never
    # reported as one.
    port_state = audit["port"]
    mapping = port_state["mapping"]
    # The listen port is what a peer connects BACK to in order to download from
    # this share: a client whose port nobody accepts on looks offline to the
    # network even while search, login and the share index all work. The audit
    # and "Test port" (/api/soulseek/port-check) answer that same question from
    # the same measurement (port_status_payload), so they must not answer it
    # differently: while the listen row says fail, the card used to answer
    # status "ok" and "other users can search, browse and download them". A
    # share a peer cannot connect back to is not "ok", and an upload served
    # yesterday does not overrule it — the listen row is about the port as it
    # is now.
    from server import soulseek_port
    listen_row = soulseek_port._listen_check(port_state)
    if listen_row["state"] == "fail":
        listen_why = ("another program is listening on" if port_state["conflict"]
                      else "nothing accepts a connection on")
        add("listen_unconfirmed", "listen_unreachable", listen_row["detail"],
            _listen_hint(port_state))
    # A gateway verdict that is not a mapping is the other case where a green
    # share lies: search, login and the index all work, so the audit used to say
    # "other users can search, browse and download them" — while a peer reaches
    # this library by connecting BACK to the listen port, and the app had just
    # been told no mapping is in place. That contradiction is the owner's report
    # (the card said shared, their client could not browse), so the mapping the
    # app ASKED for and did not get is its own status, with the remedy for the
    # install that is running (a container forwards through the host, see
    # _listen_hint). `refused`/`error` are the gateway's own refusals, which are
    # the same "no forward was confirmed" for the peer waiting to connect.
    elif mapping["enabled"] and mapping["state"] in ("refused", "no_gateway",
                                                     "unsupported", "error"):
        note(f"Peers cannot connect back to this client: {mapping['detail']} "
             f"Forward the listen port on the router (or check it on the "
             f"Soulseek page) — an unforwarded listener cannot be reached from "
             f"outside.")
        # ...unless peers have ALREADY reached it: an upload is a connection
        # they opened to this port, so a served transfer outranks the missing
        # mapping, which for a by-hand forward is only "this machine cannot see
        # the router" (a container never can).
        served = _served_uploads(cfg)
        if served:
            note(f"{served} transfer(s) have been served to other users, which "
                 f"is a connection they opened to the listen port: peers do "
                 f"reach this client, and the mapping this app asks for is not "
                 f"what is carrying them.")
        else:
            listen_why = "no forward was confirmed for"
            add("listen_unconfirmed", "listen_unreachable",
                f"Nothing confirmed a forward for the listen port "
                f"{mapping['listen_port']}: {mapping['detail']}",
                _listen_hint(port_state))
    elif mapping["enabled"] and mapping["state"] == "mapped":
        note(mapping["detail"] + f" (automatic port opening, port "
                                 f"{mapping['listen_port']}).")
    if not mapping["enabled"]:
        note("Automatic port opening is off — the listen port has to be "
             "forwarded on the router by hand for peers to reach this client.")

    live = live_share_state(cfg)
    if live is None:
        add("not_running", "share_state_unreadable",
            "slskd answered, but its share state could not be read "
            "(GET /application or /shares failed).",
            "Check the backend log — slskd may be starting or refusing the API key.")
        return finish()

    scan, live_shares, options = live["scan"], live["shares"], live["options"]
    audit["scan"].update(scan)
    audit["scan"]["log"] = _share_log_lines()
    audit["shares"]["live"] = live_shares
    if scan["faulted"]:
        audit["scan"]["state"] = "failed"
    elif scan["cancelled"]:
        audit["scan"]["state"] = "cancelled"
    elif scan["scanning"]:
        audit["scan"]["state"] = "scanning"
    elif scan["pending"]:
        audit["scan"]["state"] = "pending"
    elif scan["ready"]:
        audit["scan"]["state"] = "complete"
    else:
        audit["scan"]["state"] = "not_started"

    flags = options.get("flags") if isinstance(options.get("flags"), dict) else {}
    if flags.get("no_share_scan"):
        add("not_scanned", "scan_disabled",
            "slskd was started with `flags.no_share_scan`, so it never indexes "
            "the shared folders.",
            "That flag comes from a config this app does not generate — remove "
            "it and restart slskd.")
    if flags.get("no_connect") or flags.get("no_start"):
        add("not_logged_in", "connection_disabled",
            "slskd was started with "
            f"`flags.{'no_connect' if flags.get('no_connect') else 'no_start'}`, "
            "so it never connects to the Soulseek network.",
            "A share nobody is signed in to cannot be searched, browsed or "
            "downloaded. Remove the flag — this app never writes it — and "
            "restart slskd.")
    try:
        server = server_state(cfg) or {}
    except Exception as e:
        server = {}
        note(f"slskd's login state could not be read ({e}).")
    if server and server.get("isLoggedIn") is False:
        add("not_logged_in", "logged_out",
            "slskd is not signed in to the Soulseek network.",
            "While it is offline, nobody can find, browse or download from this "
            "share. Sign in on this page.")

    # ---- what the running daemon actually shares -------------------------- #
    def covered(path):
        """A live share entry that publishes `path` (same folder or a parent
        of it), as slskd reports it."""
        key = os.path.normcase(os.path.normpath(path))
        for row in live_shares:
            if not row["local"]:
                continue
            local = os.path.normcase(os.path.normpath(row["local"]))
            if key == local or key.startswith(local.rstrip("\\/") + os.sep):
                return row
        return None

    for path, _alias in entries:
        # an EMPTY live list is the loudest mismatch of all: the daemon is up
        # and sharing nothing, so nobody can browse anything
        row = covered(path)
        if row is None:
            audit["shares"]["mismatch"] = True
            add("config_mismatch", "share_not_live",
                f"slskd is not sharing {path} — its live share list holds "
                f"{len(live_shares)} other entr"
                f"{'y' if len(live_shares) == 1 else 'ies'}.",
                "slskd reads its share list at boot: restart it to load the "
                "config this app just generated.")
        elif row["excluded"]:
            audit["shares"]["mismatch"] = True
            add("config_mismatch", "share_excluded",
                f"slskd has {path} EXCLUDED from the share "
                f"({row['local']} with a '-' or '!' prefix).",
                "Restart slskd so it reads the config this app generates.")
    live_filters = options.get("shares", {}).get("filters") \
        if isinstance(options.get("shares"), dict) else None
    if isinstance(live_filters, list):
        ours = sorted(str(x) for x in applied)
        theirs = sorted(str(x) for x in live_filters)
        if ours != theirs:
            audit["filters"]["mismatch"] = True
            audit["shares"]["mismatch"] = True
            add("config_mismatch", "filters_stale",
                f"slskd is filtering its shares with {len(theirs)} pattern(s), "
                f"not the {len(ours)} this app generates.",
                "Restart slskd to apply the generated config (a stale filter "
                "can hide every file).")

    # ---- the scan and the index ------------------------------------------- #
    if scan["faulted"]:
        last = audit["scan"]["log"][-1] if audit["scan"]["log"] else ""
        add("scan_failed", "scan_faulted",
            "slskd's share scan failed" + (f": {last}" if last else "."),
            "The index stays empty or stale until a scan completes — press "
            "Rescan and check the log line above.")
    elif scan["cancelled"]:
        add("scan_failed", "scan_cancelled",
            "slskd's share scan was cancelled, so the index may be incomplete.",
            "Press Rescan.")
    if (scan["scanning"] or scan["pending"]) and not scan["faulted"]:
        add("scanning", "scan_in_progress",
            f"slskd is indexing the shared folders "
            f"({scan['progress']}% done) — files become visible as the scan "
            f"reaches them."
            + (" A rescan is queued." if scan["pending"] and not scan["scanning"] else ""))
    elif not scan["ready"] and not scan["files"]:
        add("not_scanned", "no_scan_yet",
            "slskd has not completed a share scan: its index holds no files.",
            "Other users find nothing until the first scan finishes — start it "
            "with Rescan and watch the log line above.")
    elif not scan["files"] and not scan["scanning"]:
        root = entries[0][0]
        add("empty_share", "index_empty",
            f"slskd's share scan reports 0 files while "
            f"{audit['disk']['audio_files']} audio file(s) sit under {root}.",
            "The usual causes are a share filter matching everything, a folder "
            "the daemon cannot read, or a download folder shared instead of the "
            "library.")
    elif scan["files"] and audit["disk"]["audio_files"] >= 20 and \
            scan["files"] < audit["disk"]["audio_files"] // 2:
        add("misconfigured", "index_undercount",
            f"slskd indexes {scan['files']} files but the shared folders hold "
            f"{audit['disk']['audio_files']} audio files.",
            "Part of the library is not being shared: an unreadable subfolder, "
            "an exclude filter, or a folder that was not in the share list when "
            "slskd scanned.")

    # ---- can a browse actually be answered? ------------------------------- #
    if probe:
        audit["browse"]["checked"] = True
        dirs, truncated, error = _share_contents(cfg)
        if error:
            audit["browse"]["ok"] = False
            audit["browse"]["detail"] = error
            add("unbrowsable", "browse_unreachable",
                f"slskd's share index did not answer ({error}).",
                "A browse request from another user fails the same way; check "
                "the backend log and that slskd's web API is up.")
        elif truncated:
            audit["browse"]["detail"] = (
                f"the share index is larger than the {_PROBE_MAX_BYTES // (1024 * 1024)} "
                f"MB this check reads, so it was not verified")
        elif not dirs:
            audit["browse"]["ok"] = False
            add("unbrowsable", "browse_empty",
                "slskd's share index answered with no folders at all.",
                "Nothing is in the index: see the scan state above.")
        elif not audit["disk"]["probe_file"]:
            audit["browse"]["detail"] = ("no audio file was found on disk to look "
                                         "for in the index")
        else:
            audit["browse"]["directories"] = len(dirs)
            found, detail = _index_has_file(dirs, newest[2] or entries[0][0],
                                            audit["disk"]["probe_file"])
            audit["browse"]["ok"] = found
            audit["browse"]["detail"] = detail
            if not found:
                add("unbrowsable", "file_not_in_index",
                    f"The index slskd serves does not hold a file that is on "
                    f"disk: {detail}.",
                    "Other users browsing this share do not see that file — "
                    "rescan, and check the filters and permissions on its folder.")

    return finish()


# --------------------------------------------------------------------------- #
# Share refreshes: keeping the network's view of the library current
# --------------------------------------------------------------------------- #
# A run — an import chain, a tag batch, an optimize pass, a library organize —
# rewrites hundreds of paths in minutes. slskd indexes shares at boot and only
# picks changes up from a rescan, so without this other users keep browsing the
# file list the library had when the daemon last started.
_SHARE_REFRESH_LOCK = threading.Lock()
_SHARE_REFRESH_TIMER = {"timer": None}


def refresh_shares_soon(delay=6.0):
    """Coalesce a burst of library changes into ONE share rescan.

    Trailing-edge debounce: every call pushes the timer out, so a run that
    keeps writing files asks slskd once, a few seconds after the last change,
    instead of once per album. Cheap enough to call from anywhere.
    """
    with _SHARE_REFRESH_LOCK:
        timer = _SHARE_REFRESH_TIMER["timer"]
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(delay, _refresh_shares_now)
        timer.daemon = True
        _SHARE_REFRESH_TIMER["timer"] = timer
    timer.start()


def _refresh_shares_now():
    with _SHARE_REFRESH_LOCK:
        _SHARE_REFRESH_TIMER["timer"] = None
    cfg = load_config()
    try:
        if not (is_running() or web_up(cfg)):
            return  # nothing to tell; the next start indexes the current tree
        try:
            rescan_shares(cfg)
        except Exception:
            # Builds without the rescan API (and a daemon that is up but not
            # answering the call) need the restart path — the same thing the
            # manual "Refresh shares" button does.
            traceback.print_exc()
            restart(cfg)
    except Exception:
        traceback.print_exc()


# --------------------------------------------------------------------------- #
# Importing completed downloads into the library
# --------------------------------------------------------------------------- #
# An album folder is one that holds audio (LIB_AUDIO_EXTS — music videos are
# tracks too) or the rip evidence that always travels with it.
_ALBUM_FILE_EXTS = frozenset(LIB_AUDIO_EXTS) | {".log", ".cue"}


def _is_album_file(name):
    return os.path.splitext(name)[1].lower() in _ALBUM_FILE_EXTS


def _holds_audio(path):
    """True when `path` holds an audio file anywhere below (dot-dirs pruned).

    A top-level folder holding only rip evidence — a peer's stray `<name>.log`
    and cues with no track — is a leftover, not an album: moving it in
    published a library album named after the peer's folder and with nothing
    to play. Audio anywhere below still means album (a disc subfolder, a
    music video).
    """
    for _root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if any(os.path.splitext(f)[1].lower() in LIB_AUDIO_EXTS for f in files):
            return True
    return False


def _holds_album_files(path, direct_only=False):
    """True when `path` holds an album file (itself, or anywhere below).

    Dot-dirs are pruned either way. Partials now live OUTSIDE this tree (a
    sibling `incomplete/` — see _incomplete_dir), so this is belt-and-braces
    for an install that has not migrated yet and still has slskd staging them
    in `.incomplete/<user>/…`: a half-downloaded file must never turn a folder
    into an album.
    """
    if direct_only:
        try:
            names = os.listdir(path)
        except OSError:
            return False
        return any(_is_album_file(n) and os.path.isfile(os.path.join(path, n))
                   for n in names)
    for _root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if any(_is_album_file(f) for f in files):
            return True
    return False


def _prune_incomplete(ddir):
    """Delete the empty staging trees slskd leaves in the incomplete dir.

    A finished or rejected transfer leaves `<incomplete>/<user>/<remote dir>`
    behind and slskd never cleans it up, so the staging dir accumulated a
    forest of empty directories. `rmdir` bottom-up only ever removes
    directories that hold nothing — a tree still carrying a partial file
    survives untouched — and the root itself stays (slskd validates it at
    boot, and recreates any directory it needs to write into).

    Takes the DOWNLOAD dir, which is what the callers hold, and derives the
    incomplete dir from it — they are siblings (see _incomplete_dir). A
    pre-migration `<downloads>/.incomplete` is pruned too, so a half-migrated
    install still gets its empty staging trees cleaned up."""
    roots = [_incomplete_dir_for(ddir)]
    legacy = os.path.join(ddir, ".incomplete")
    if os.path.isdir(legacy) and legacy not in roots:
        roots.append(legacy)
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, dirs, _files in os.walk(root, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(base, d))
                except OSError:
                    pass


def prune_download_dirs(ddir):
    """Delete the empty user/batch/album shells left under the download dir.

    slskd creates the directories of a transfer's destination (see
    DESTINATION_SUBDIR: `<ddir>/<user>/<batch id>/<remote path>/…`) and never
    removes one again, so a rejected candidate, a cancelled job and a
    successful import each left an empty chain behind. `rmdir` bottom-up only
    ever removes a directory that holds NOTHING — a partial, a stray file or a
    dot-dir keeps its parents alive — and the download root itself is never
    touched (slskd validates that one exists at boot).

    Dot-dirs are skipped: `.incomplete` is slskd's staging tree and belongs to
    _prune_incomplete, which keeps that root by design."""
    if not ddir or not os.path.isdir(ddir):
        return
    empties = []
    for base, dirs, _files in os.walk(ddir):
        # topdown keeps the walk out of dot-dirs; the list is built
        # parents-first, so it is walked in REVERSE to empty a chain bottom-up
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        empties += [os.path.join(base, d) for d in dirs]
    for path in reversed(empties):
        try:
            os.rmdir(path)
        except OSError:
            pass


def _incomplete_dir_for(downloads):
    """The incomplete dir belonging to *downloads*: its sibling `incomplete`.

    One rule, shared with _incomplete_dir — the path the app generates into
    slskd's config is exactly the path read back out here, so a report or a
    prune can never disagree with where slskd is really writing."""
    from mlo.paths import INCOMPLETE_DIR_NAME
    parent = os.path.dirname(os.path.abspath(downloads)) or downloads
    return os.path.join(parent, INCOMPLETE_DIR_NAME)


def clear_transfer_files(ddir, username, filename, size=0):
    """Delete the local partial bytes ONE slskd transfer left behind.

    Returns {"files_deleted", "bytes_freed", "problems"}: `problems` holds the
    one thing a caller can report as a failure (a file that would not delete —
    slskd still holding it open is expected), and nothing here ever raises.

    A file under a staging dir is unfinished by definition. In the DOWNLOAD dir
    the only honest test is size: slskd moves a finished transfer into place,
    so a file already at the peer's reported size may be the album ITSELF (it
    is, for a succeeded transfer) — refused, deliberately without a problem, so
    clearing a completed download can never delete it. Only a short file there
    is a truncated leftover from a cancelled/failed attempt.

    Paths are the ones the auto-importer resolves: the destination template is
    `<downloads>/<user>/<batch id>/<remote path>/<file>` (see
    DESTINATION_SUBDIR) while a staged partial keeps the remote relative path
    under `incomplete/` — and the same roots are checked again for an install
    that has not migrated yet and still stages into `<downloads>/.incomplete`,
    or still has files at the older `${SOURCE_DIRECTORY}` shapes."""
    from server.soulseek_auto import _leaf_of, _remote_rel
    out = {"files_deleted": 0, "bytes_freed": 0, "problems": []}
    rel = _remote_rel(str(filename or ""))
    if not (ddir and rel):
        return out
    user = str(username or "").strip()
    base = rel.rsplit("/", 1)[-1]
    leaf = _leaf_of(rel)
    # every shape slskd may have written the bytes at: the full remote path,
    # the old `${SOURCE_DIRECTORY}` pattern's `<leaf>/<file>`, and a flat
    # `<file>`, each with and without the username segment slskd adds on its
    # own
    shapes = [rel, base]
    if leaf:
        shapes += [os.path.join(leaf, base)]
    if user:
        shapes += [os.path.join(user, s) for s in list(shapes)]
    roots = [(ddir, False),
             (_incomplete_dir_for(ddir), True),
             (os.path.join(ddir, ".incomplete"), True)]
    paths = []
    for root, staged in roots:
        for shape in shapes:
            paths.append((os.path.normpath(os.path.join(root, shape)), staged))
    # the CURRENT layout adds slskd's batch id between the user and the remote
    # path, which no fixed shape can name: the user's own subtree is searched
    # once for the file's basename (a bounded walk, never the whole download
    # dir). The size test below still decides — only a file SHORTER than the
    # transfer's reported size is a leftover here.
    user_root = os.path.join(ddir, user)
    if user and _inside(user_root, ddir):
        incomplete = (os.sep + ".incomplete").lower()
        for base_dir, dirs, files in os.walk(user_root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if incomplete in base_dir.lower():
                continue
            for f in files:
                if os.path.normcase(f) == os.path.normcase(base):
                    paths.append((os.path.normpath(os.path.join(base_dir, f)), False))
    seen = set()
    for p, staged in paths:
        if p in seen:
            continue
        seen.add(p)
        if not os.path.isfile(p):
            continue
        try:
            on_disk = os.path.getsize(p)
        except OSError:
            continue          # renamed/removed under us: nothing to delete
        if not staged and not (size and on_disk < int(size)):
            continue          # possibly the completed download itself
        try:
            os.remove(p)
        except OSError as e:
            out["problems"].append(f"{p}: {e}")
            continue
        out["files_deleted"] += 1
        out["bytes_freed"] += on_disk
    if out["files_deleted"]:
        _prune_incomplete(ddir)
        # the album/batch/user chain the deleted file leaves behind, when
        # nothing else is in it
        prune_download_dirs(ddir)
    return out


def _move(src, dst):
    """Move one path with mlo.paths.move_path -> (ok, reason).

    move_path retries a sharing violation (WinError 32) and NEVER falls back
    to copy+delete for a same-volume directory, which is what turned a file
    slskd still had open into a half-imported album with real duplicate bytes
    on both sides (and a hard failure for an import that had already
    copied everything)."""
    from mlo.paths import move_path
    notes = []
    ok = bool(move_path(src, dst, log=notes.append))
    return ok, (notes[-1] if notes else "move did not complete")


def _path_components(path, root):
    """`path` below `root` as lowercase components, () when it is not below it.

    The one spelling every identity comparison in this file uses: a real path
    is compared as the tuple of its parts so "inside the download dir" and
    "which peer's tree" are decidable, where a string prefix/leaf test is not
    (the same leaf sits under every peer, and a sibling folder can share it).
    """
    try:
        rel = os.path.relpath(os.path.abspath(os.path.normpath(str(path))),
                              os.path.abspath(os.path.normpath(str(root))))
    except (OSError, ValueError):
        return ()
    rel = rel.replace("\\", "/")
    if rel in ("", ".") or rel.startswith("../") or rel == "..":
        return ()
    return tuple(x.lower() for x in rel.split("/") if x not in ("", "."))


def _pending_album_folders(cfg=None):
    """The REMOTE folders with running transfers — `{(peer, folder path)}`.

    A folder's identity is the peer slskd reports AND the transfer's own remote
    folder path below the share root (as `_path_components`, lowercased). That
    pair names exactly one directory on disk: the pinned destination template
    puts the download at `<ddir>/<peer>/<batch id>/<that path>` (see
    DESTINATION_SUBDIR), which is the same tail `soulseek_auto._candidate_dirs`
    matches a candidate's own folders by.

    The LEAF alone was not an identity: every peer and every batch has its own
    folder of any given name, so an unrelated peer's unfinished transfer made a
    complete album look busy (and a finished download's own still-running
    sibling under another peer/batch let it be moved). `_still_downloading`
    therefore compares both halves, stepping over the one component between the
    peer and the folder path — the batch id, which slskd keeps to itself and
    never reports in this tree.
    """
    from server.soulseek_auto import _remote_rel
    try:
        tree = downloads_state(cfg) or []
    except Exception:
        # slskd unreachable: refusing every import would be worse than moving
        # one folder early, and the UI already reports slskd as down.
        return set()
    pending = set()
    for user in tree:
        if not isinstance(user, dict):
            continue
        who = str(user.get("username") or "").strip().lower()
        for d in user.get("directories") or []:
            if not isinstance(d, dict):
                continue
            for f in d.get("files") or []:
                if not isinstance(f, dict) or finished_transfer(f.get("state")):
                    continue
                # `_remote_rel` is already a clean relative path joined with
                # "/": the folder the file lands in is it minus the file name.
                rel = _remote_rel(str(f.get("filename") or ""))
                folder = tuple(x.lower() for x in rel.split("/")[:-1] if x)
                if not folder:
                    # A file at the top of the peer's share has no folder below
                    # the batch id: the batch directory itself is where it
                    # lands, and slskd reports no id to name it by. Left out,
                    # as the old leaf rule left it out.
                    continue
                pending.add((who, folder))
    return pending


def _still_downloading(src, ddir, pending):
    """True when a folder about to be moved holds a live transfer's own folder.

    A multi-disc album is moved as ONE `Album` folder while the transfers still
    coming down sit in its `CD1`/`CD2`, so every directory inside `src` counts,
    not only `src` itself.

    Each candidate is named the way its download named it (see
    `_pending_album_folders`: the peer slskd reports and the remote folder path
    below the share root), against the path it has below the download dir:

    * deeper than the folder path — `<peer>/<batch id>/<folder path>…` (the
      pinned destination template) or `<peer>/<folder path>…`: the peer is the
      first component and the folder path the tail, with slskd's own batch id
      the one component in between;
    * exactly the folder path — `<folder path>…`: the layout that kept the
      peer's own directory structure and nothing else;
    * shallower — the layers that dropped components below the leaf (slskd's
      old `${SOURCE_DIRECTORY}` default): there is no path and often no peer
      left to compare, so the leaf is all the identity those folders ever had.
    """
    if not pending:
        return False
    for base, _dirs, _files in os.walk(src):
        rel = _path_components(base, ddir)
        if not rel:
            continue
        for peer, folder in pending:
            if len(rel) > len(folder):
                if rel[0] == peer and rel[-len(folder):] == folder:
                    return True
            elif len(rel) == len(folder):
                if rel == folder:
                    return True
            elif rel[-1] == folder[-1]:
                return True
    return False


# Album folders the LAST import_completed() left in the download dir (still
# downloading). Module-level because the return value is the moved list every
# caller already unpacks.
_last_import_skipped: list = []
# Albums the last import_completed() could not move (a file slskd still holds
# open): [{path, reason}] — the response route turns these into per-album
# errors instead of a bare 500.
_last_import_failed: list = []
# Folders left alone because they carry rip evidence (.log/.cue) and no audio.
_last_import_leftovers: list = []
# finish_album() results of the last import_completed(finish=True): one entry
# per moved album, so a caller can report which scripts ran and what failed.
_last_import_scripts: list = []


def last_import_skipped():
    """Paths of the albums import_completed() refused to move (still
    downloading), in walk order."""
    return list(_last_import_skipped)


def last_import_failed():
    """Albums the last import_completed() tried and failed to move:
    [{"path", "reason"}]. `path` is where the album still sits (inside the
    download dir), `reason` is slskd's/the filesystem's own complaint."""
    return [dict(x) for x in _last_import_failed]


def last_import_leftovers():
    """Download-dir folders the last import_completed() left in place because
    they hold no audio at all (a peer's stray .log/.cue folder)."""
    return list(_last_import_leftovers)


def last_import_scripts():
    """The import chain results of the last import_completed(finish=True):
    [{"path", "scripts", "chain", "errors"}], one per moved album, [] when no
    chain was run. A script failure lands in `errors` — the album is imported
    either way."""
    return [dict(x) for x in _last_import_scripts]


def import_completed(cfg=None, finish=False, progress=None):
    """Move completed downloads into the library root, one album per folder.

    Every album lands in `<music folder>/Artists` — never the music folder
    root, which is shared to the network and must not publish an album the
    organizer has not renamed into place yet.

    With *finish* true, each moved album then runs the configured import chain
    (``server.imports.finish_album``) — the same chain every other import path
    runs, reported per album by last_import_scripts(). It is failure-tolerant:
    a failing script is reported, the album stays imported. Off by default
    because the chain belongs AFTER the caller's own convert / MEDIA / organize
    steps (the naming script renames the album folder, and grading an
    unorganized album is a false verdict).

    slskd's completed layout is `<download dir>/<user>/<batch id>/<remote
    path>/<album>/<file>` (see DESTINATION_SUBDIR — the batch id is slskd's
    own, one per enqueue call, so no two candidates share a root). Each
    top-level entry is classified:

      * holds an album file directly -> ONE album folder, moved whole;
      * holds only rip evidence (a peer's `.log`/`.cue`) and no audio ->
        leftover: left in the download dir and reported by
        last_import_leftovers();
      * holds no album file of its own but album folders below it -> a
        container (the per-user, per-batch and peer-path levels the staging
        layout nests): the folders inside it are imported, and any loose file
        beside them becomes a "Soulseek <container>" album;
      * a loose file -> gathered into a "Soulseek" album.

    A folder whose children are all disc folders (`…/Album/CD1` + `…/CD2`) is
    taken as the ONE album it is, not as two albums named after the discs.

    Every move goes through mlo.paths.move_path, so a file slskd still holds
    open is retried and then reported instead of degrading into copy+delete
    (or killing the whole call): such an album stays in the download dir, does
    NOT appear in the returned list, and lands in last_import_failed() as
    {"path", "reason"}. An album whose transfers are still running comes back
    from last_import_skipped(). Dot-dirs are never moved (a pre-migration
    `.incomplete/` may still sit here), and the empty staging trees under the
    incomplete dir — and the emptied containers left under the download dir —
    are pruned. Returns the list of new album paths.
    """
    cfg = cfg or load_config()
    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        raise ValueError("music_folder not set or not found")
    ddir = download_dir(cfg)
    global _last_import_skipped, _last_import_failed, _last_import_leftovers
    global _last_import_scripts
    _last_import_skipped = []
    _last_import_failed = []
    _last_import_leftovers = []
    _last_import_scripts = []
    if not os.path.isdir(ddir):
        return []

    pending = _pending_album_folders(cfg)
    _prune_incomplete(ddir)

    def unique_album_dir(name):
        # Straight into the library root (<music>/Artists), never the music
        # folder root: that one is shared to the network, and an import that
        # organize has not renamed into place yet must not be published.
        root = library_root(folder)
        os.makedirs(root, exist_ok=True)
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name)).strip().rstrip(".")
        dest = os.path.join(root, safe or "Soulseek Download")
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(root, f"{safe} ({n})")
            n += 1
        return dest

    moved = []

    def still_downloading(src):
        """True when a folder about to be moved holds a live transfer's own
        folder (see `_still_downloading` and `_pending_album_folders`)."""
        return _still_downloading(src, ddir, pending)

    def take_album(src):
        """Move one album folder whole — unless it is still downloading."""
        if still_downloading(src):
            _last_import_skipped.append(src)
            return
        dest = unique_album_dir(os.path.basename(src))
        ok, why = _move(src, dest)
        if ok:
            moved.append(dest)
        else:
            # it is still in the download dir and still named by nothing:
            # `moved` only ever holds albums that really arrived
            _last_import_failed.append({"path": src, "reason": why})

    def gather(files, src_dir, name):
        """Loose files -> one album folder."""
        if not files:
            return
        dest = unique_album_dir(name)
        os.makedirs(dest)
        landed = 0
        for f in files:
            src_file = os.path.join(src_dir, f)
            ok, why = _move(src_file, os.path.join(dest, f))
            if ok:
                landed += 1
            else:
                _last_import_failed.append({"path": src_file, "reason": why})
        if not landed:
            try:
                os.rmdir(dest)   # empty: our own folder, nothing arrived
            except OSError:
                pass
            return
        moved.append(dest)

    def disc_parent(path):
        """True when every album-bearing child of `path` is a DISC folder
        (…/Album/CD1 + …/Album/CD2): `path` is then the album, not its
        discs."""
        from server.soulseek_auto import _disc_number
        children = [c for c in sorted(os.listdir(path))
                    if not c.startswith(".")
                    and os.path.isdir(os.path.join(path, c))
                    and _holds_album_files(os.path.join(path, c))]
        return bool(children) and all(_disc_number(c) for c in children)

    def take_tree(epath):
        """Import the album folders below a CONTAINER directory, then any loose
        file sitting beside them.

        A directory holding an album file directly is an album; one whose
        children are all disc folders is its album's parent. Anything else is a
        container — a per-user folder, a batch id, or a level of the peer's own
        share path — and the walk follows it down until it reaches albums, so
        the staging layout's depth needs no special case here."""
        for c in sorted(os.listdir(epath)):
            if c.startswith("."):
                continue
            cpath = os.path.join(epath, c)
            if not os.path.isdir(cpath) or os.path.islink(cpath):
                # a loose file, or a link (never descended: a junction back
                # into the tree would walk for ever)
                continue
            if not _holds_album_files(cpath):
                continue          # scans/ with no album file: not an album
            if not _holds_audio(cpath):
                # rip evidence with no audio anywhere below it: a peer's stray
                # log/cue folder, which must never publish an album
                _last_import_leftovers.append(cpath)
                continue
            if _holds_album_files(cpath, direct_only=True) or disc_parent(cpath):
                take_album(cpath)
            else:
                take_tree(cpath)
        gather([f for f in os.listdir(epath)
                if os.path.isfile(os.path.join(epath, f))],
               epath, f"Soulseek {os.path.basename(epath)}")

    loose = []
    for entry in sorted(os.listdir(ddir)):
        # slskd stages every in-progress transfer in
        # <downloads>/.incomplete/<user>/<remote dir>/<file>. Treating that
        # tree as a peer folder moved half-downloaded files into the library
        # as albums named after peers and rmdir'ed slskd's staging dir out
        # from under the running transfers. Same for any other dot-dir.
        if entry.startswith("."):
            continue
        epath = os.path.join(ddir, entry)
        if not os.path.isdir(epath):
            loose.append(entry)
            continue
        if not _holds_album_files(epath):
            # nothing album-shaped anywhere below: left alone, never swept in
            continue
        if not _holds_audio(epath):
            # rip evidence with no audio anywhere below it: a peer's stray
            # log/cue folder, which must never publish an album
            _last_import_leftovers.append(epath)
            continue
        if _holds_album_files(epath, direct_only=True) or disc_parent(epath):
            take_album(epath)
            continue
        # No album file of its own: a container (the per-user / batch id /
        # peer path levels of the staging layout). Its albums are imported by
        # walking down to them; anything with no album file anywhere below is
        # left alone rather than swept into the library.
        take_tree(epath)
        try:
            os.rmdir(epath)
        except OSError:
            pass  # an album-less child (scans/, a stray folder) keeps it alive
    # loose files directly in the download root -> one album folder
    gather(loose, ddir, "Soulseek")
    # the user/batch/album shells the moves above emptied (a leftover or a
    # failed move keeps its own chain alive — rmdir only takes empty dirs)
    prune_download_dirs(ddir)
    if finish and moved:
        # The configured chain, once per imported album. A failure is recorded
        # (last_import_scripts) and never loses the import: the albums are in
        # the library and are returned either way.
        from server import imports
        for album in moved:
            try:
                _last_import_scripts.append(
                    imports.finish_album(album, cfg, progress=progress))
            except Exception:
                traceback.print_exc()
                _last_import_scripts.append({"path": album, "scripts": [],
                                             "chain": [], "errors":
                                             ["import chain crashed"]})
    return moved


def ready_albums(cfg=None):
    """Album folders in the download dir that `import_completed()` would move.

    This is the list the Downloads page and the "Import all completed" button
    work from, so it must answer the SAME question the mover does — minus the
    moving. That means the same walk and the same rules: an album is a folder
    holding an audio file (directly, or as its ONE parent when every child is
    a disc folder), a folder whose transfers are still running is not ready,
    and a folder holding only rip evidence is a leftover rather than an album.

    Nothing here moves, renames or prunes anything: it is a read, and it is
    called from a polling endpoint, so an incomplete download must simply be
    absent from the list until slskd says it finished.
    """
    cfg = cfg or load_config()
    ddir = download_dir(cfg)
    if not ddir or not os.path.isdir(ddir):
        return []
    pending = _pending_album_folders(cfg)

    def still_downloading(src):
        return _still_downloading(src, ddir, pending)

    def disc_parent(path):
        from server.soulseek_auto import _disc_number
        children = [c for c in sorted(os.listdir(path))
                    if not c.startswith(".")
                    and os.path.isdir(os.path.join(path, c))
                    and _holds_album_files(os.path.join(path, c))]
        return bool(children) and all(_disc_number(c) for c in children)

    out = []

    def walk(epath):
        """Collect the album folders below `epath`, exactly as the importer
        classifies them (see import_completed's take_tree)."""
        for c in sorted(os.listdir(epath)):
            if c.startswith("."):
                continue
            cpath = os.path.join(epath, c)
            if not os.path.isdir(cpath) or os.path.islink(cpath):
                continue
            if not _holds_album_files(cpath) or not _holds_audio(cpath):
                continue
            if _holds_album_files(cpath, direct_only=True) or disc_parent(cpath):
                if not still_downloading(cpath):
                    out.append(cpath)
            else:
                walk(cpath)

    for entry in sorted(os.listdir(ddir)):
        if entry.startswith("."):
            continue
        epath = os.path.join(ddir, entry)
        if not os.path.isdir(epath):
            continue  # a loose file: the importer gathers those, nothing to name here
        if not _holds_album_files(epath) or not _holds_audio(epath):
            continue
        if _holds_album_files(epath, direct_only=True) or disc_parent(epath):
            if not still_downloading(epath):
                out.append(epath)
            continue
        walk(epath)
    return out
