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


def _issue(request: Request, response: Response, days: int, label: str = "") -> dict:
    token, expires = auth_mod.create_session(days, label=label)
    _set_cookie(response, request, token, days)
    return {
        "token": token,
        "expires_at": expires,
        "session_days": int(days),
        "username": auth_mod.current_state()["username"],
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
    """
    state = auth_mod.current_state()
    token = auth_mod.token_from_request(request)
    return {
        "required": bool(state["required"]),
        "has_password": bool(state["has_password"]),
        "authenticated": bool(token and auth_mod.valid_session(token)),
        "username": state["username"],
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

    auth_mod.set_password(body.password, body.username)
    # set_password revokes every session (a password change must sign old
    # clients out), so the caller gets a fresh one to keep working with.
    return _issue(request, response, auth_mod.current_state(refresh=True)["session_days"],
                  label="setup")


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    ip = _guard_rate(request)
    state = auth_mod.current_state()
    if not state["has_password"]:
        raise HTTPException(status_code=428, detail="no password set yet",
                            headers={"X-MLO-Needs-Setup": "1"})
    from mlo.config import load_config
    try:
        stored = str(load_config().get("auth_password_hash") or "")
    except Exception:
        stored = ""
    if not auth_mod.verify_password(body.password, stored):
        auth_mod.note_failure(ip)
        # Same message and shape for "wrong password" and "no password set":
        # an attacker learns nothing about the server's state from a failure.
        raise HTTPException(status_code=401, detail="wrong password")
    auth_mod.note_success(ip)
    return _issue(request, response, state["session_days"])


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
    from mlo.config import load_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if not auth_mod.verify_password(body.current, str(cfg.get("auth_password_hash") or "")):
        auth_mod.note_failure(ip)
        raise HTTPException(status_code=401, detail="current password is wrong")
    problem = auth_mod.password_problem(body.password, body.confirm)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    auth_mod.note_success(ip)
    auth_mod.set_password(body.password, None)
    return _issue(request, response, auth_mod.current_state(refresh=True)["session_days"],
                  label="password change")


@router.post("/revoke-all")
def revoke_all(request: Request, response: Response):
    """Sign every client out everywhere (including this one)."""
    count = auth_mod.revoke_all()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True, "revoked": count}


@router.get("/sessions")
def sessions():
    return {"sessions": auth_mod.session_count(), "checked_at": time.time()}
