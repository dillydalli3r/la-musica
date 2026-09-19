"""Login gate: one password, hashed on disk, sessions in SQLite.

The server is an appliance for one household, not a hosting platform: there is
no signup, no roles and no per-user permissions. What it needs is the *gate*:
anyone who can reach the port can otherwise read the whole library, edit tags,
delete files and start downloads, so as soon as the backend is reachable from
anywhere but this machine's own loopback it must demand a password before
doing any of that. The `users` table is the identity a session carries, so the
playlists, likes and favorites can be scoped per person; `""` is the
default/admin scope — the one an unclaimed install uses, and where every row
written before that table existed lives.

Everything here is deliberately small and boring:

* **The password** is stored as PBKDF2-HMAC-SHA256 (`pbkdf2$<rounds>$<salt>$<hash>`)
  in the config's `auth_password_hash`. Never the password itself, and the
  comparison is constant-time.
* **Sessions** are random 32-byte tokens. Only the SHA-256 of a token is
  stored (`auth.db`, next to playlists.db), so reading that file does not hand
  anyone a working login. They expire (`auth_session_days`) and are pruned on
  every write. A session also carries the username it was opened for, which is
  what `current_user(request)` hands to the rest of the API.
* **Users** are `(username, hash, created)` rows in the same database. The
  config's `auth_password_hash`/`auth_username` stay the claim of an install
  that predates the table (and the display name), so an old install logs in
  exactly as before.
* **Brute force** is answered with per-client backoff: a handful of wrong
  passwords from the same address and that address waits, doubling up to a
  ceiling. A correct password clears it.
* **Transport**: the token travels as `Authorization: Bearer <token>` (the
  clients), as an HttpOnly `mlo_session` cookie (the browser, where
  `<audio src=…>` and `<img src=…>` cannot send a header) or as `?token=`
  (WebSockets and the Tauri/mobile shells, whose origin is not the API's).
  That last form is the one compromise in this file: a token in a URL can end
  up in a log or a history entry, so it is only accepted where the other two
  cannot be used.

`auth_mode: auto` (the default) is what decides whether the gate applies at
all: ON whenever `server_host` is not a loopback address, OFF for loopback.
`required` gates loopback too; `off` never turns the gate off for a
non-loopback bind — that combination is a misconfiguration, not a choice, and
is treated as `required` with a warning.
"""

import hashlib
import hmac
import ipaddress
import os
import secrets
import sqlite3
import threading
import time

# OWASP's current floor for PBKDF2-HMAC-SHA256. The round count is written
# into the stored string, so raising this constant later still verifies the
# hashes written before it.
_PBKDF2_ROUNDS = 600_000
_MIN_PASSWORD_LEN = 8

# Wrong-password backoff: after this many consecutive failures from one
# address, that address waits `_BACKOFF_BASE_S`, doubling per further failure
# up to `_BACKOFF_MAX_S`. A success clears the record, so a user who mistypes
# once and then gets it right is never punished.
_BACKOFF_AFTER = 5
_BACKOFF_BASE_S = 30
_BACKOFF_MAX_S = 900

_lock = threading.RLock()
_init_lock = threading.Lock()
_initialized = False
_failures = {}  # ip -> (count, locked_until)


def db_path():
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "auth.db")


def _conn():
    global _initialized
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    if not _initialized:
        with _init_lock:
            if not _initialized:
                _initialized = True  # set first — _init() re-enters _conn()
                try:
                    _init()
                except Exception:
                    _initialized = False
                    raise
    return conn


def _init():
    with _lock:
        conn = _conn()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    label      TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    hash     TEXT,
                    created  REAL NOT NULL
                );
                """
            )
            # Migrations for databases written before users existed: the
            # session's username is added in place, and rows that predate it
            # read back as "" — the default/admin scope.
            for stmt in ("ALTER TABLE sessions ADD COLUMN username TEXT",):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()
        finally:
            conn.close()


# ── password ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """`pbkdf2$<rounds>$<salt-hex>$<hash-hex>` for `password`."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a stored hash.

    An absent or unparsable hash is "no password set" (False), never an
    accidental pass — the caller decides what that means (first-run setup).
    """
    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2":
        return False
    try:
        rounds = int(parts[1])
        salt = bytes.fromhex(parts[2])
        want = bytes.fromhex(parts[3])
    except ValueError:
        return False
    if rounds <= 0 or not salt or not want:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(got, want)


def password_problem(password: str, confirmation: str = None) -> str:
    """Why the password cannot be used, or "" when it can.

    Kept as a string, not a bool: the message is what the setup form shows,
    and having one place that decides means the API, the CLI and the tests
    cannot disagree about what "too short" means.
    """
    if not password or not str(password).strip():
        return "password is empty"
    if len(str(password)) < _MIN_PASSWORD_LEN:
        return f"password must be at least {_MIN_PASSWORD_LEN} characters"
    if confirmation is not None and str(password) != str(confirmation):
        return "passwords do not match"
    return ""


# ── users ───────────────────────────────────────────────────────────────────

def list_users() -> list:
    """Every claimed username, oldest first (the default scope is "")."""
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT username FROM users ORDER BY created, username").fetchall()
            return [str(r["username"]) for r in rows]
        finally:
            conn.close()


def user_hash(username: str) -> str:
    """The stored hash for `username`, or "" when there is no such row."""
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT hash FROM users WHERE username=?",
                               (str(username or "").strip(),)).fetchone()
            return str(row["hash"] or "") if row else ""
        finally:
            conn.close()


def user_problem(username) -> str:
    """Why *username* cannot be a user, or "" when it can.

    A username is not just a label: it is a SQLite key and a **folder name**
    (the trash bin is `<music>/.mlo/trash/<user>/`), so a name carrying a
    separator, a traversal segment or a control character would write outside
    the bin it names. Rejected here, once, for every caller.
    """
    name = str(username or "").strip()
    if not name:
        return "a username cannot be empty"
    if len(name) > 64:
        return "a username is at most 64 characters"
    if any(ord(c) < 32 for c in name):
        return "a username cannot contain control characters"
    if "/" in name or "\\" in name or name in (".", ".."):
        return "a username cannot contain a path separator"
    return ""


def create_user(username, password) -> str:
    """Add a user, or change an existing one's password. Returns the name.

    Deliberately different from `set_password`: adding a second person must
    not sign the first one out, and must not rewrite the config's own claim
    (which is the display name, and the only credential an install that
    predates the users table has). The caller checks the password rules and
    the name with `password_problem` / `user_problem`.
    """
    name = str(username or "").strip()
    stored = hash_password(password)
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("UPDATE users SET hash=? WHERE username=?", (stored, name))
            if not cur.rowcount:
                conn.execute(
                    "INSERT INTO users (username, hash, created) VALUES (?, ?, ?)",
                    (name, stored, time.time()))
            conn.commit()
        finally:
            conn.close()
    current_state(refresh=True)
    return name


def delete_user(username) -> bool:
    """Remove a user and every session opened for them.

    The LAST user is refused: with no users left the server falls back to the
    config's claim, so "delete the only user" would silently change which
    password opens the library instead of closing it. Emptying the server is
    a config operation, not a user one. Their rows stay in the databases —
    this removes the identity, not the data someone may still want to export.
    """
    name = str(username or "").strip()
    if not name or name not in list_users():
        return False
    if len(list_users()) <= 1:
        return False
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM users WHERE username=?", (name,))
            conn.execute("DELETE FROM sessions WHERE username=?", (name,))
            conn.commit()
        finally:
            conn.close()
    current_state(refresh=True)
    return True


def _config_claim() -> tuple:
    """`(username, hash)` as the config records them.

    The claim of an install that predates the users table, and still the
    display name a client is shown; login falls back to it so such an install
    keeps working without a users row.
    """
    try:
        from mlo.config import load_config
        cfg = load_config()
    except Exception:
        return "", ""
    return (str(cfg.get("auth_username") or "").strip(),
            str(cfg.get("auth_password_hash") or ""))


def login_user(password: str, username: str = None):
    """The username `password` logs in as, or None when it does not match.

    Two places a credential can live, and the order matters:

    1. **A users row**: authoritative for the name it holds. Naming a user
       logs in as them; naming nobody on a single-user server logs in as the
       only one there is (which is what every client does — the name field is
       optional).
    2. **The config's own claim**, which is the DEFAULT scope's credential:
       `""` for an install claimed without a name, and the `auth_username` of
       one claimed before the users table existed. It is consulted only for a
       name no row holds, so a stale config hash can never override a row.

    The second case is what keeps an install reachable: claim with no name,
    add a second user, and the original password must still open the default
    scope — otherwise the first person's playlists, likes and favourites are
    behind a name nobody can type.
    """
    want = str(username or "").strip()
    names = list_users()
    if want and want in names:
        stored = user_hash(want)
        return want if stored and verify_password(password, stored) else None
    if not want and len(names) == 1:
        # Only a MATCH returns here: a single user whose password does not
        # match must still fall through to the config's claim, or adding one
        # named user would lock the original owner out of the default scope.
        stored = user_hash(names[0])
        if stored and verify_password(password, stored):
            return names[0]
    claim_name, claim_hash = _config_claim()
    if want in ("", claim_name) and claim_name not in names \
            and claim_hash and verify_password(password, claim_hash):
        return claim_name
    return None


# ── sessions ────────────────────────────────────────────────────────────────

def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def create_session(days=30, label: str = "", username: str = "") -> tuple:
    """A new session token. Returns `(token, expires_at)` — the caller gets
    the token once and cannot read it back: only its hash is stored. The row
    carries the username the session was opened for, which is what the API
    scopes everything by ("" = the default/admin scope)."""
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = 30
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + days * 86400
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT OR REPLACE INTO sessions"
                " (token_hash, created_at, expires_at, label, username)"
                " VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), now, expires, label or "",
                 str(username or "").strip()),
            )
            conn.commit()
        finally:
            conn.close()
    return token, expires


def session_username(token: str) -> str:
    """The user this LIVE session was opened for, "" when it is not a session.

    An expired token answers "" here (and is left for the next write to
    prune); callers that only need "is this a session at all" use
    `valid_session`.
    """
    if not token:
        return ""
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT username FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (_token_hash(token), time.time()),
            ).fetchone()
            return str(row["username"] or "") if row else ""
        finally:
            conn.close()


def valid_session(token: str) -> bool:
    """Is this token a live session? Expired rows are refused (and dropped)."""
    if not token:
        return False
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT expires_at FROM sessions WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
            if not row:
                return False
            if float(row["expires_at"]) <= time.time():
                conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                             (_token_hash(token),))
                conn.commit()
                return False
            return True
        finally:
            conn.close()


def revoke_session(token: str) -> None:
    if not token:
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            conn.commit()
        finally:
            conn.close()


def revoke_all() -> int:
    """Sign every client out (password change, "log out everywhere")."""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("DELETE FROM sessions")
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


def session_count() -> int:
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE expires_at > ?", (time.time(),)
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()


# ── brute-force backoff ─────────────────────────────────────────────────────

def retry_after(ip: str) -> int:
    """Seconds this client must wait before another attempt (0 = may try)."""
    with _lock:
        count, until = _failures.get(ip or "", (0, 0.0))
        remaining = int(max(0.0, until - time.time()))
        # Drop the record once the wait is over and the window has cooled, so
        # a stale counter cannot accumulate over days of light mistyping.
        if remaining == 0 and count and until and until < time.time() - 3600:
            _failures.pop(ip or "", None)
        return remaining


# The failure map is keyed by client address, i.e. by something an attacker
# controls: a port-forwarded install sees every scanner on the internet, and a
# single IPv6 /64 is enough to mint unbounded distinct keys. The backoff only
# ever needs the addresses that are still trying, so the map is swept (and
# hard-capped) rather than left to grow for the life of the process.
_MAX_FAILURES = 4096


def _evict_stale_failures(now: float) -> None:
    stale = [k for k, (count, until) in _failures.items()
             if until and until < now - 3600]
    for k in stale:
        _failures.pop(k, None)
    if len(_failures) > _MAX_FAILURES:
        # Still too many: keep the ones with a live lock (they are the active
        # attackers) and drop the rest.
        for k in [k for k, (_c, until) in _failures.items() if not until]:
            _failures.pop(k, None)


def note_failure(ip: str) -> None:
    with _lock:
        _evict_stale_failures(time.time())
        count, _ = _failures.get(ip or "", (0, 0.0))
        count += 1
        until = 0.0
        if count >= _BACKOFF_AFTER:
            delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2 ** (count - _BACKOFF_AFTER)))
            until = time.time() + delay
        _failures[ip or ""] = (count, until)


def note_success(ip: str) -> None:
    with _lock:
        _failures.pop(ip or "", None)


# ── request plumbing ────────────────────────────────────────────────────────

def client_ip(request) -> str:
    """The address to rate-limit on.

    `X-Forwarded-For` is deliberately NOT trusted: this server is reached
    directly (or through a reverse proxy the user runs), and honouring a
    header a client can set would let one attacker pose as thousands of
    addresses and skip the backoff entirely.
    """
    try:
        return str(request.client.host or "")
    except Exception:
        return ""


def normalize_public_url(url: str) -> str:
    """A configured `server_public_url` as an address a client can dial.

    The wizard and Settings accept a bare host ("music.example.com:8443"), so
    the server normalises rather than handing a scheme-less string to a
    client: https when the port is 443, http otherwise. A value that already
    carries a scheme — and "" (no public URL configured) — is returned as it
    is, minus a trailing slash.
    """
    text = str(url or "").strip().rstrip("/")
    if not text or "://" in text:
        return text
    from urllib.parse import urlsplit
    try:
        # Parsed as a netloc so a bracketed IPv6 host reads its port too.
        port = urlsplit("//" + text).port
    except ValueError:
        port = None
    return ("https://" if port == 443 else "http://") + text


def is_loopback_host(host: str) -> bool:
    """True for "127.0.0.1", "::1", "localhost" and the rest of 127/8."""
    text = str(host or "").strip().lower()
    if text in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def gate_required(cfg: dict) -> bool:
    """Does this configuration demand a login?

    `auth_mode: auto` follows the bind address; `required` always; `off` only
    ever for a loopback bind (a non-loopback bind with `off` is treated as
    `required` and reported by `gate_warning`).
    """
    mode = str(cfg.get("auth_mode") or "auto").lower()
    loopback = is_loopback_host(cfg.get("server_host"))
    if mode == "off":
        return not loopback
    if mode == "required":
        return True
    return not loopback


def gate_warning(cfg: dict) -> str:
    """A sentence for the log/UI when the configuration is unsafe, else ""."""
    mode = str(cfg.get("auth_mode") or "auto").lower()
    if mode == "off" and not is_loopback_host(cfg.get("server_host")):
        return ("auth_mode is \"off\" but the server binds "
                f"{cfg.get('server_host')!r}; the login gate stays ON because "
                "that address is reachable from the network.")
    if not is_loopback_host(cfg.get("server_host")) and not cfg.get("auth_password_hash"):
        return ("the server binds a non-loopback address with no password set; "
                "only the first-run setup endpoint answers until one is set.")
    return ""


def token_from_request(request) -> str:
    """The session token carried by an HTTP request, from any of the three
    places it is allowed to travel (header, cookie, query)."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    cookie = request.cookies.get("mlo_session")
    if cookie:
        return cookie
    return (request.query_params.get("token") or "").strip()


def current_user(request) -> str:
    """The user behind this request, "" for the default/admin scope.

    "" covers both "no session" (a loopback install with the gate off) and a
    session opened before the server had users; both own the rows and files
    written before user scoping existed, so they must keep seeing them.
    """
    try:
        return session_username(token_from_request(request))
    except Exception:
        return ""


# ── gate state ──────────────────────────────────────────────────────────────

# The gate runs on EVERY API request, including a media stream's range
# requests (a player issues many per track), and reading config.json per
# request would put a file read on that path. The state is therefore cached
# for a couple of seconds — short enough that turning the gate on in Settings
# takes effect before the user can switch back to a client, long enough that a
# seek storm costs one read.
_STATE_TTL_S = 3.0
_state = {"at": 0.0}


def cached_state():
    """The gate state when the cache is still fresh, else None.

    The middleware calls this first: the common case (a request within the
    3-second window) must not pay a thread hop or a config read at all, and
    only a cold cache goes to the (blocking) `current_state`.
    """
    if time.time() - float(_state.get("at") or 0.0) < _STATE_TTL_S and _state.get("at"):
        return dict(_state)
    return None


def current_state(refresh: bool = False) -> dict:
    """`{required, has_password, username, host, public_url, session_days}`.

    Never raises: an unreadable config answers "gate off, no password", which
    keeps a broken config from locking the owner out of their own server.
    """
    now = time.time()
    if not refresh and now - float(_state.get("at") or 0.0) < _STATE_TTL_S:
        return dict(_state)
    try:
        from mlo.config import load_config
        cfg = load_config()
    except Exception:
        cfg = {}
    _state.update(
        at=now,
        required=gate_required(cfg),
        has_password=bool(cfg.get("auth_password_hash")),
        username=str(cfg.get("auth_username") or ""),
        host=str(cfg.get("server_host") or ""),
        public_url=normalize_public_url(cfg.get("server_public_url") or ""),
        session_days=int(cfg.get("auth_session_days") or 30),
    )
    return dict(_state)


def set_password(password: str, username: str = None) -> str:
    """Store a new password hash and return the username it was stored for.

    The hash lives in the users table (the row is created for a new user) and
    the config keeps its copy as the display/migration source. `username=None`
    keeps the config's own `auth_username` — the claim of an install that
    predates the users table. The cached gate state is invalidated so the next
    request sees the change, and every session is revoked (a password change
    must sign old clients out).
    """
    from mlo.config import load_config, save_config
    cfg = load_config()
    stored = hash_password(password)
    name = (str(cfg.get("auth_username") or "").strip() if username is None
            else str(username).strip())
    problem = user_problem(name)
    if problem and name:
        raise ValueError(problem)
    with _lock:
        conn = _conn()
        try:
            # A BLANK claim creates no users row. It is what a fresh install
            # produces — the name field is optional and empty on a server that
            # has never had one — and a row named "" would be the original
            # owner's identity: as soon as a second user existed, `login_user`
            # would refuse the ambiguous blank name and their playlists, likes
            # and favourites would be unreachable behind a name nobody can
            # type. With no row the config's own hash stays the credential,
            # which is the branch `login_user` already authenticates.
            if name:
                cur = conn.execute("UPDATE users SET hash=? WHERE username=?", (stored, name))
                if not cur.rowcount:
                    conn.execute("INSERT INTO users (username, hash, created) VALUES (?, ?, ?)",
                                 (name, stored, time.time()))
            conn.commit()
        finally:
            conn.close()
    cfg["auth_password_hash"] = stored
    if username is not None:
        cfg["auth_username"] = name
    save_config(cfg)
    current_state(refresh=True)
    revoke_all()
    return name


# Paths that answer without a session. Everything else under the API does not.
# `/api/health` is what the launchers (tray.py, start_app.py, the Tauri shell)
# probe to prove the port is OURS before adopting or killing it, and the
# login screen needs `/api/auth/status` before it has a token. The SPA shell
# itself is public by design: it is the same bytes for everyone and contains
# no library data.
PUBLIC_PATHS = (
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/setup",
)

# The API's own documentation routes sit OUTSIDE /api, so the "everything
# else is the static shell" rule would publish them: /docs, /redoc and
# /openapi.json are a complete map of every route, parameter and body a
# password-protected server exposes, and there is no reason to hand that to an
# unauthenticated caller. They follow the gate like any other API surface.
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


# Everything outside /api is the static shell: the same bytes for everyone,
# carrying no library data, and what the login screen itself is made of.
def is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path in _DOC_PATHS:
        return False
    return not path.startswith("/api")
