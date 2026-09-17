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


def generate_yaml(cfg=None):
    """Render slskd config; returns (yaml_text, api_key)."""
    cfg = cfg or load_config()
    api_key = uuid.uuid4().hex
    username = str(cfg.get("soulseek_username") or "").strip()
    password = str(cfg.get("soulseek_password") or "").strip()
    description = str(cfg.get("soulseek_description") or "").strip()
    listen_port = int(cfg.get("soulseek_listen_port") or 50000)
    web_port = int(cfg.get("soulseek_web_port") or 5030)
    up_limit = int(cfg.get("soulseek_up_limit") or 0)
    down_limit = int(cfg.get("soulseek_down_limit") or 0)
    downloads = download_dir(cfg)
    shared = share_dirs(cfg)
    exclude = share_exclude(cfg)

    lines = [
        "# Generated by la musica — edits are overwritten.",
        "web:",
        f"  port: {web_port}",
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
    lines += [
        "soulseek:",
        f"  username: {_yq(username)}",
        f"  password: {_yq(password)}",
        f"  description: {_yq(description)}",
        "  listen_ip_address: 0.0.0.0",
        f"  listen_port: {listen_port}",
        f"  global_upload_limit: {up_limit}",
        f"  global_download_limit: {down_limit}",
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
    if want and got and got.lower() != want.lower():
        return False, got, (f"another application's slskd is already using "
                            f"port {p} (signed in as {got})")
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
        if proc is None or proc.poll() is not None:
            # untracked (adopted) slskd — the only way down is via the port
            return _kill_port_listener(port) if instance_owner(cfg)[0] else False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except OSError:
            pass
    # a straggler or adopted sibling can still hold the web port
    time.sleep(0.3)
    if instance_owner(cfg)[0]:
        _kill_port_listener(port)
    # the pooling client holds sockets to a dead slskd (and its API key)
    _close_client()
    return True


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


def _request(method, path, json_body=None, timeout=30.0):
    client = _http_client()
    # per-request timeout: the shared client keeps the 30 s default, every
    # call keeps overriding it exactly as it did when it built its own client
    r = client.request(method, path, json=json_body, headers=_headers(),
                       timeout=timeout)
    if r.status_code in (401, 403) and _proc["api_key"]:
        # stale key from a previous slskd boot — the running instance
        # mints its own; retry anonymously (auth is localhost-only)
        _proc["api_key"] = None
        r = client.request(method, path, json=json_body, headers=_headers(),
                           timeout=timeout)
    r.raise_for_status()
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


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
def search(query, cfg=None, timeout_ms=None):
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
    outright (this is what starved candidate discovery in auto-import)."""
    search_id = str(uuid.uuid4())
    body = {"id": search_id, "searchText": query}
    if timeout_ms:
        # slskd's DTO field is SearchTimeout; a "timeout" key is ignored.
        body["searchTimeout"] = max(1, int(timeout_ms))
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


def enqueue_download(username, files, cfg=None):
    """Queue files for download: files = [{filename, size}].

    slskd's enqueue route is per-user (POST /transfers/downloads/{username})
    with a bare list body — there is no top-level /downloads route."""
    body = [{"filename": f["filename"], "size": int(f.get("size") or 0)}
            for f in files]
    _request("POST", f"/transfers/downloads/{quote(str(username), safe='')}",
             json_body=body, timeout=30.0)
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
    slskd omits it (a transfer it has not started measuring yet)."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        pct = (100.0 * done / size) if size else 0.0
    return max(0, min(100, int(round(pct))))


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


def cancel_downloads(username, transfer_ids, cfg=None):
    """Cancel + untrack downloads of one user (DELETE /transfers/downloads/…).

    slskd's cancel route is per transfer and takes an optional ?remove=true
    ("also drop it from the tracked list"). A rejected/errored transfer stays
    queued otherwise, so slskd re-requests the very files whose partials the
    auto-importer just deleted. Best effort: a transfer that is already gone
    answers 404/400 and must not abort the run."""
    user = quote(str(username), safe="")
    for tid in transfer_ids:
        if not tid:
            continue
        try:
            _request("DELETE",
                     f"/transfers/downloads/{user}/{quote(str(tid), safe='')}?remove=true",
                     timeout=15.0)
        except Exception:
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


def _incomplete_dir_for(downloads):
    """The incomplete dir belonging to *downloads*: its sibling `incomplete`.

    One rule, shared with _incomplete_dir — the path the app generates into
    slskd's config is exactly the path read back out here, so a report or a
    prune can never disagree with where slskd is really writing."""
    from mlo.paths import INCOMPLETE_DIR_NAME
    parent = os.path.dirname(os.path.abspath(downloads)) or downloads
    return os.path.join(parent, INCOMPLETE_DIR_NAME)


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

    The generated slskd config sets no destination pattern, so slskd uses its
    default `${SOURCE_DIRECTORY}`: a completed transfer lands in
    `<download dir>/<leaf of the remote folder>/<file>`. A folder carrying
    that leaf name therefore belongs to a live transfer and must not be moved
    into the library yet (the legacy layout nests it under the username, but
    the leaf is the same).
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


def import_completed(cfg=None):
    """Move completed downloads into the library root, one album per folder.

    Every album lands in `<music folder>/Artists` — never the music folder
    root, which is shared to the network and must not publish an album the
    organizer has not renamed into place yet.

    slskd's completed layout is `<download dir>/<leaf of the remote folder>/
    <file>` — its default destination pattern is `${SOURCE_DIRECTORY}`, which
    this app does not override. So each top-level entry is classified:

      * holds audio anywhere inside -> ONE album folder, moved whole;
      * holds only rip evidence (a peer's `.log`/`.cue`) and no audio ->
        leftover: left in the download dir and reported by
        last_import_leftovers();
      * holds no audio but has audio-bearing children -> the legacy per-user
        layout: every child is an album, leftover loose files become
        "Soulseek <user>";
      * a loose file -> gathered into a "Soulseek" album.

    Every move goes through mlo.paths.move_path, so a file slskd still holds
    open is retried and then reported instead of degrading into copy+delete
    (or killing the whole call): such an album stays in the download dir, does
    NOT appear in the returned list, and lands in last_import_failed() as
    {"path", "reason"}. An album whose transfers are still running comes back
    from last_import_skipped(). Dot-dirs are never moved (a pre-migration
    `.incomplete/` may still sit here), and the empty staging trees under the
    incomplete dir are pruned. Returns the list of new album paths.
    """
    cfg = cfg or load_config()
    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        raise ValueError("music_folder not set or not found")
    ddir = download_dir(cfg)
    global _last_import_skipped, _last_import_failed, _last_import_leftovers
    _last_import_skipped = []
    _last_import_failed = []
    _last_import_leftovers = []
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

    def take_album(src):
        """Move one album folder whole — unless it is still downloading."""
        if os.path.basename(src).lower() in pending:
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
        if not _holds_audio(epath) and _holds_album_files(epath):
            # rip evidence with no audio anywhere below it: a peer's stray
            # log/cue folder, which must never publish an album
            _last_import_leftovers.append(epath)
            continue
        if _holds_album_files(epath, direct_only=True):
            take_album(epath)
            continue
        # No album file of its own: only a legacy per-user folder can still
        # hold albums (its children), and anything without a single album file
        # below it is left alone rather than swept into the library.
        children = {}
        for c in sorted(os.listdir(epath)):
            cpath = os.path.join(epath, c)
            if not c.startswith(".") and os.path.isdir(cpath):
                children[c] = _holds_audio(cpath)
        albums = [c for c, audio in children.items() if audio]
        if not albums:
            continue
        for c, audio in children.items():
            if not audio and _holds_album_files(os.path.join(epath, c)):
                _last_import_leftovers.append(os.path.join(epath, c))
        for child in albums:
            take_album(os.path.join(epath, child))
        gather([f for f in os.listdir(epath)
                if os.path.isfile(os.path.join(epath, f))],
               epath, f"Soulseek {entry}")
        try:
            os.rmdir(epath)
        except OSError:
            pass  # an album-less child (scans/, a stray folder) keeps it alive
    # loose files directly in the download root -> one album folder
    gather(loose, ddir, "Soulseek")
    return moved
