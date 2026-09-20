"""`/api/auth/*` — first-run setup, login, logout, password change.

The gate itself is `server/auth.py` (and the middleware in `server/main.py`);
this module is the HTTP surface around it, kept in its own router for the same
reason the discovery/lyrics routers are: the logic is testable without the
whole app.

The flow on a fresh remote install:

    GET  /api/auth/status   -> {"required": true, "has_password": false, …}
    POST /api/auth/setup    -> sets the first password, returns a session
    POST /api/auth/login    -> returns a session for later visits

Until a password exists, and the gate applies, every other API route answers
428 with `needs_setup: true`, so a client can tell "sign in" apart from
"nobody has claimed this server yet" instead of showing a password box that
cannot possibly work.
"""

import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from server import auth as auth_mod

router = APIRouter(prefix="/api/auth", tags=["auth"])

# The cookie name the browser sends back on every same-origin request — which
# is what lets `<audio src="/api/stream?...">` and `<img src="/api/cover?...">`
# be authorized at all, since neither can carry an Authorization header.
COOKIE_NAME = "mlo_session"


class SetupBody(BaseModel):
    password: str
    confirm: str = None
    username: str = None


class LoginBody(BaseModel):
    password: str
    # Optional: omitted means "the only user there is", which is what every
    # existing client sends (and what a single-user install wants).
    username: str = None


class UserBody(BaseModel):
    username: str
    password: str
    confirm: str = None


class PasswordBody(BaseModel):
    current: str = ""
    password: str
    confirm: str = None


def _set_cookie(response: Response, request: Request, token: str, days: int) -> None:
    """Set the session cookie for the browser.

    `secure` follows the request's own scheme so the same code works on
    plain-http LAN installs (where a `secure` cookie would simply be dropped
    and media playback would silently 401) and behind TLS. `samesite="lax"` is
    enough: every request that needs the cookie is same-origin, and a lax
    cookie is not sent on cross-site form posts, which is the CSRF case that
    matters here.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(days) * 86400,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        path="/",
    )


def _issue(request: Request, response: Response, days: int, label: str = "",
           username: str = "") -> dict:
    token, expires = auth_mod.create_session(days, label=label, username=username)
    _set_cookie(response, request, token, days)
    return {
        "token": token,
        "expires_at": expires,
        "session_days": int(days),
        "username": str(username or ""),
    }


def _guard_rate(request: Request) -> str:
    ip = auth_mod.client_ip(request)
    wait = auth_mod.retry_after(ip)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"too many attempts — try again in {wait}s",
            headers={"Retry-After": str(wait)},
        )
    return ip


@router.get("/status")
def status(request: Request):
    """Everything a client needs to decide what to show, and nothing secret.

    Public by design: the login screen has to render before anyone has a
    token. It never reports whether a guess was close — only whether a
    password exists at all.

    `required` answers THIS request: a client reaches the server from another
    device and must sign in, while the machine the server runs on (its own
    browser, a desktop shell, the host of a container) never does — see
    auth.local_addresses. `gate` is the server-wide answer behind it, which is
    what a sign-in-and-security panel should show, and `local` says which of
    the two this client is.
    """
    state = auth_mod.current_state()
    token = auth_mod.token_from_request(request)
    needs_login = auth_mod.requires_login(request, state)
    local = auth_mod.is_local_request(request)
    return {
        "required": bool(needs_login),
        "gate": bool(state["required"]),
        "local": bool(local),
        "has_password": bool(state["has_password"]),
        # A local client is never asked for a session, so `authenticated` would
        # read false on a server whose gate is on for the network — and the
        # shell would show a login screen nobody needs. NOT NEEDING TO SIGN IN
        # is the client's answer: the default scope answers for it, exactly as
        # it did before the gate existed.
        "authenticated": bool(token and auth_mod.valid_session(token)) or not needs_login,
        # The signed-in user's own name; before that it is the config's
        # display name (the claim that gave this server its password).
        "username": auth_mod.session_username(token) or state["username"],
        "public_url": state["public_url"],
        "session_days": int(state["session_days"]),
        "setup_hint": auth_mod.gate_warning(_config_for_hint()),
    }


def _config_for_hint() -> dict:
    """The two keys `gate_warning` reads, without re-reading the whole config
    on every status poll (the status endpoint is the one route clients hit
    before they have anything cached)."""
    from mlo.config import load_config
    try:
        cfg = load_config()
    except Exception:
        return {}
    return {"auth_mode": cfg.get("auth_mode"), "server_host": cfg.get("server_host"),
            "auth_password_hash": cfg.get("auth_password_hash")}


@router.post("/setup")
def setup(body: SetupBody, request: Request, response: Response):
    """Claim the server: set the first password.

    Refuses (409) once a password exists unless the caller is already signed
    in — otherwise "setup" would be a password-reset endpoint for anyone who
    can reach the port, which is exactly what the gate is for.
    """
    state = auth_mod.current_state(refresh=True)
    token = auth_mod.token_from_request(request)
    signed_in = bool(token and auth_mod.valid_session(token))
    if state["has_password"] and not signed_in:
        raise HTTPException(status_code=409, detail="this server already has a password")

    problem = auth_mod.password_problem(body.password, body.confirm)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    try:
        name = auth_mod.set_password(body.password, body.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # set_password revokes every session (a password change must sign old
    # clients out), so the caller gets a fresh one to keep working with.
    return _issue(request, response, auth_mod.current_state(refresh=True)["session_days"],
                  label="setup", username=name)


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    ip = _guard_rate(request)
    state = auth_mod.current_state()
    if not state["has_password"]:
        raise HTTPException(status_code=428, detail="no password set yet",
                            headers={"X-MLO-Needs-Setup": "1"})
    user = auth_mod.login_user(body.password, body.username)
    if user is None:
        auth_mod.note_failure(ip)
        # Same message and shape for "wrong password", "no such user" and "no
        # password set": an attacker learns nothing about the server's state.
        raise HTTPException(status_code=401, detail="wrong password")
    auth_mod.note_success(ip)
    return _issue(request, response, state["session_days"], username=user)


@router.post("/logout")
def logout(request: Request, response: Response):
    token = auth_mod.token_from_request(request)
    auth_mod.revoke_session(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordBody, request: Request, response: Response):
    """Change the password. Requires the current one, and re-issues the
    caller's own session so they are not signed out of the tab they are in."""
    ip = _guard_rate(request)
    user = auth_mod.current_user(request)
    if auth_mod.login_user(body.current, user) is None:
        auth_mod.note_failure(ip)
        raise HTTPException(status_code=401, detail="current password is wrong")
    problem = auth_mod.password_problem(body.password, body.confirm)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    auth_mod.note_success(ip)
    # The same user whose password was just proved — never the config's
    # display name, which on a multi-user server is not who is asking.
    try:
        name = auth_mod.set_password(body.password, user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _issue(request, response, auth_mod.current_state(refresh=True)["session_days"],
                  label="password change", username=name)


@router.get("/users")
def users(request: Request):
    """Every user on this server, and which one is asking.

    The default/admin scope ("") is not a user row and is not listed: it is
    where an unclaimed install's playlists, likes and favourites live, and the
    login screen already offers it to whoever has no name of their own.
    """
    return {"users": auth_mod.list_users(),
            "you": auth_mod.current_user(request)}


@router.post("/users")
def add_user(body: UserBody):
    """Add a user, or set an existing one's password — a server operator act.

    Behind the same gate as changing a password (it needs a session), but
    deliberately different from `/password`: it does NOT sign anyone out, and
    it does not touch the config's own claim, because adding a second person
    must not disconnect the first.
    """
    name = str(body.username or "").strip()
    problem = auth_mod.user_problem(name)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if body.confirm is not None and body.password != body.confirm:
        raise HTTPException(status_code=400, detail="the passwords do not match")
    problem = auth_mod.password_problem(body.password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    auth_mod.create_user(name, body.password)
    return {"ok": True, "username": name, "users": auth_mod.list_users()}


@router.delete("/users/{username}")
def remove_user(username: str):
    """Remove a user and every session they hold.

    The last user is refused — with no users left the server falls back to its
    config claim, so removing the only one would change which password opens
    the library rather than closing it. The rows they own stay on disk.
    """
    if not auth_mod.delete_user(username):
        raise HTTPException(
            status_code=400,
            detail="cannot remove that user: it is the last one, or it does not exist")
    return {"ok": True, "users": auth_mod.list_users()}


@router.post("/revoke-all")
def revoke_all(request: Request, response: Response):
    """Sign every client out everywhere (including this one)."""
    count = auth_mod.revoke_all()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True, "revoked": count}


@router.get("/sessions")
def sessions():
    return {"sessions": auth_mod.session_count(), "checked_at": time.time()}
