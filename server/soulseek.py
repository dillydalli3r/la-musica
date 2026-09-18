"""Managed slskd (Soulseek) integration.

slskd (https://github.com/slskd/slskd) is a single-binary Soulseek client
with a REST API. It is vendored into .dependencies like every other tool;
this module:

  * generates slskd's YAML config from MLO settings (credentials, profile
    description, listen/web ports, upload/download limits, shares = the
    music folder, download dir),
  * spawns/monitors the process,
  * exposes a thin REST client (search, downloads, transfers, profile).

The generated config lives at <music folder>/.mlo/data/slskd.yaml; a random
web API key is minted per start and kept in memory (the web UI is bound to
localhost).
"""
import os
import re
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
]


def share_dirs(cfg=None):
    """Folders shared to the Soulseek network (default: the music folder)."""
    cfg = cfg or load_config()
    dirs = [str(d).strip() for d in (cfg.get("soulseek_share_dirs") or []) if str(d).strip()]
    music = str(cfg.get("music_folder") or "").strip()
    if not dirs and music:
        dirs = [music]
    return dirs


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
    """Extra share exclude regexes from settings, on top of the reserved ones."""
    cfg = cfg or load_config()
    extra = [str(x).strip() for x in (cfg.get("soulseek_share_exclude") or []) if str(x).strip()]
    out = _RESERVED_SHARE_FILTERS + [f"'{x}'" for x in extra]
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
    dl_slots = max(1, min(20, int(cfg.get("soulseek_download_slots") or 3)))
    ul_slots = max(0, min(20, int(cfg.get("soulseek_upload_slots") or 2)))
    up_kib = max(0, int(cfg.get("soulseek_upload_limit_kib") or 0))
    down_kib = max(0, int(cfg.get("soulseek_download_limit_kib") or 0))
    downloads = download_dir(cfg)
    shared = share_dirs(cfg)
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
    if username:
        lines += [
            "authentication:",
            f"  username: {_yq(username)}",
            f"  password: {_yq(password)}",
        ]
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
    if shared and cfg.get("soulseek_share_library", True):
        lines += ["shares:", "  directories:"]
        lines += [f"    - {_yq(d)}" for d in shared]
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
    log = os.path.join(os.path.dirname(config_path()), "slskd.log")
    try:
        with open(log, "rb") as f:
            lines = [l.strip() for l in
                     f.read().decode("utf-8", "replace").splitlines() if l.strip()]
    except OSError:
        return ""
    for line in reversed(lines):
        if any(lvl in line for lvl in _LOG_LEVELS) and \
                any(k in line.lower() for k in _LOGIN_KEYS):
            return line.split("] ", 1)[-1].strip()[:300]
    return ""


def _options_dirs(cfg=None):
    """slskd's live download dirs from GET /options, or {} when unreadable."""
    try:
        client = _http_client(cfg)
        r = client.get("/options", headers={"Accept": "application/json"},
                       timeout=1.0)
        if r.status_code != 200:
            return {}
        return (r.json() or {}).get("directories") or {}
    except Exception:
        return {}


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
    return {
        "state": st_dict.get("state"),
        "isComplete": bool(st_dict.get("isComplete")),
        "responseCount": int(st_dict.get("responseCount") or 0),
        "fileCount": int(st_dict.get("fileCount") or 0),
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


def uploads_state(cfg=None):
    """Full upload transfer tree (what others have downloaded = shared
    history), grouped per user. Same shape as downloads_state()."""
    try:
        return _request("GET", "/transfers/uploads") or []
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise


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
    """Ask slskd to rescan its share index (PUT /shares)."""
    _request("PUT", "/shares", timeout=30.0)
    return True


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


def _pending_album_folders(cfg=None):
    """Leaf names of remote folders whose transfers are still running.

    The destination template keeps the remote folder structure below the
    username and the batch id (see DESTINATION_SUBDIR), so the LEAF of a
    transfer's remote path is still the name of the local directory the file
    lands in — slskd recreates that directory wherever the rest of the path
    puts it. `take_album` therefore matches a name anywhere inside the folder
    it is about to move (a multi-disc album is moved as one `Album` folder
    while its running transfers sit in `CD1`/`CD2`).
    """
    from server.soulseek_auto import _remote_rel
    try:
        tree = downloads_state(cfg) or []
    except Exception:
        # slskd unreachable: refusing every import would be worse than moving
        # one folder early, and the UI already reports slskd as down.
        return set()
    leaves = set()
    for user in tree:
        if not isinstance(user, dict):
            continue
        for d in user.get("directories") or []:
            if not isinstance(d, dict):
                continue
            for f in d.get("files") or []:
                if not isinstance(f, dict) or finished_transfer(f.get("state")):
                    continue
                rel = _remote_rel(str(f.get("filename") or ""))
                leaf = os.path.basename(os.path.dirname(rel.replace("/", os.sep)))
                if leaf:
                    leaves.add(leaf.strip().lower())
    return leaves


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
        """True when a folder about to be moved holds a remote folder whose
        transfers are still running.

        A multi-disc album is moved as ONE `Album` folder while the transfers
        still coming down sit in its `CD1`/`CD2`, so matching the moved
        folder's own name is not enough: every directory name inside it counts
        (the remote folder's leaf is the local directory name — one per
        running file)."""
        if not pending:
            return False
        for base, _dirs, _files in os.walk(src):
            if os.path.basename(base).lower() in pending:
                return True
        return False

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
