"""MusicBrainz OAuth2 login + rating SUBMISSIONS — the one WRITE call.

Every MusicBrainz READ the app makes (search, browse, release lookups, cover
art, aliases — all of ``server.integrations``) is anonymous: MusicBrainz needs
no account for any of it. Writing the user's OWN rating back is the one call
that needs their account, and this module owns that half:

  * the OAuth2 **connect** dance (PKCE, public/installed client, the OOB code
    the user pastes back) and its **finish**, plus the token RENEWAL the
    short-lived access token (``expires_in`` 3600) forces on every later call;
  * ``whoami`` — which account the saved login belongs to, from
    ``GET /oauth2/userinfo`` (``server.credential_checks`` calls this);
  * ``submit_ratings`` — ONE MMD-2.0 rating request for a whole album's
    recordings plus its release group (an album is many recordings, and one
    request is the point), through
    ``POST https://musicbrainz.org/ws/2/rating``.

The wire shape is MusicBrainz's own and is pinned in ``tools/test_mb_submit.py``:

  * values live on a **20-point grid** (20 per star, 0 = REMOVE the rating).
    The app's ratings are half-stars (0-10) and its file ``RATING`` tags are
    Picard's 0-100; either is snapped to the grid HALF-UP (``round`` is
    banker's and would answer 40 for 50 — see ``server.ratings.from_tag`` for
    the same rule applied the other way). A value that would snap to 0 is
    NEVER sent: 0 deletes the user's rating on MusicBrainz, and nothing in
    this app may ever mean that;
  * the body is the MMD-2.0 namespace's ``<metadata>`` holding one
    ``<recording-list>`` / ``<release-group-list>`` (or the other allowed
    kinds) with a ``<user-rating>`` per entity, HTTPS only, carrying the app's
    own User-Agent beside the Bearer token.

Refusals are the service's OWN words — an HTTP status and body, or a
``<error>`` element inside a 200 — and are RETURNED as text, never raised: a
submission failing must degrade to "not sent, here is why", never take a route
down with it.
"""
import base64
import hashlib
import re
import secrets
import time
import urllib.parse

import httpx

from mlo.config import save_config

# The endpoints (all HTTPS; MusicBrainz serves nothing on plain HTTP) and the
# OOB redirect a public/installed client registers
# (musicbrainz.org/account/applications).
AUTHORIZE_URL = "https://musicbrainz.org/oauth2/authorize"
TOKEN_URL = "https://musicbrainz.org/oauth2/token"
USERINFO_URL = "https://musicbrainz.org/oauth2/userinfo"
RATING_URL = "https://musicbrainz.org/ws/2/rating"
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
# The one scope a rating submission needs, and the whole reason for the login.
SCOPE = "rating"

# MMD-2.0: the namespace every rating body carries. MusicBrainz rejects a body
# without it.
MMD_NS = "http://musicbrainz.org/ns/mmd-2.0#"

# The config keys this module owns (all shipped by mlo/config.py).
CLIENT_ID_KEY = "musicbrainz_client_id"
CLIENT_SECRET_KEY = "musicbrainz_client_secret"
REFRESH_KEY = "musicbrainz_refresh_token"
ACCESS_KEY = "musicbrainz_access_token"
EXPIRES_KEY = "musicbrainz_token_expires_at"
USERNAME_KEY = "musicbrainz_username"
SUBMIT_KEY = "musicbrainz_submit_enabled"
# The in-flight PKCE halves of a Connect that has not finished yet: `connect_url`
# writes them so a client may hand back NOTHING but the code, and finish/
# disconnect clear them. They are never shown in Settings (like the app's other
# PKCE state, they are a single-use handshake value).
PKCE_VERIFIER_KEY = "musicbrainz_pkce_verifier"
PKCE_STATE_KEY = "musicbrainz_pkce_state"

# An access token is renewed this long BEFORE it expires, so a request built a
# moment before the deadline never races it.
_EXPIRY_SKEW = 60.0
_TIMEOUT = 20.0

# Every entity MusicBrainz accepts a user-rating for, and the MMD element
# names each one posts under. There is deliberately NO "release": MusicBrainz
# rates a release GROUP, and an album's own verdict belongs to the group.
_ENTITIES = {
    "artist": ("artist-list", "artist"),
    "label": ("label-list", "label"),
    "recording": ("recording-list", "recording"),
    "release-group": ("release-group-list", "release-group"),
    "work": ("work-list", "work"),
}
# The element order lists are written in — stable, so one call's body is
# byte-identical from run to run (the wire shape is pinned in the test).
_ENTITY_ORDER = ("artist", "label", "recording", "release-group", "work")

_MBID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ERROR_RE = re.compile(r"<error>(.*?)</error>", re.S)
# The userinfo payload's username lives under whichever of these MusicBrainz
# states; `sub` is the OIDC subject and IS the MusicBrainz account name.
_NAME_KEYS = ("preferred_username", "username", "sub")


# --------------------------------------------------------------------------- #
# HTTP seams — the two calls this module makes, each returning
# `(answer, refusal)`: the refusal is MusicBrainz's own text, or "" when it
# answered.
# --------------------------------------------------------------------------- #
def _user_agent():
    """The app's own User-Agent, read from integrations at call time.

    Read through the module rather than copied, so the string MusicBrainz
    etiquette asked us to send is the one the reads already send."""
    from server.integrations import USER_AGENT
    return USER_AGENT


def _post_form(url, data, timeout=None):
    """One form-encoded POST (the OAuth2 token endpoint). -> (body, refusal)."""
    try:
        r = httpx.post(url, data=data,
                       headers={"User-Agent": _user_agent()},
                       timeout=timeout or _TIMEOUT)
    except Exception as e:
        return None, f"no answer ({type(e).__name__}: {e})"
    text = (r.text or "").strip()
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {text[:300]}"
    try:
        return r.json(), ""
    except Exception:
        return None, (f"MusicBrainz answered HTTP {r.status_code} but not "
                      f"JSON: {text[:300]}")


def _get_userinfo(token, timeout=None):
    """`GET /oauth2/userinfo` with the Bearer token. -> (body, refusal)."""
    try:
        r = httpx.get(USERINFO_URL,
                      headers={"Authorization": f"Bearer {token}",
                               "User-Agent": _user_agent()},
                      timeout=timeout or _TIMEOUT)
    except Exception as e:
        return None, f"no answer ({type(e).__name__}: {e})"
    text = (r.text or "").strip()
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {text[:300]}"
    try:
        return r.json(), ""
    except Exception:
        return None, (f"MusicBrainz answered HTTP {r.status_code} but not "
                      f"JSON: {text[:300]}")


def _post_rating(body, token, timeout=None):
    """One rating POST. -> (answer_text, refusal); a refusal inside a 200
    (MusicBrainz's `<error>` element) counts as a refusal too."""
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/xml; charset=utf-8",
               "User-Agent": _user_agent()}
    try:
        r = httpx.post(RATING_URL, content=body.encode("utf-8"),
                       headers=headers, timeout=timeout or _TIMEOUT)
    except Exception as e:
        return "", f"no answer ({type(e).__name__}: {e})"
    text = (r.text or "").strip()
    if r.status_code >= 400:
        return "", f"HTTP {r.status_code}: {text[:300]}"
    m = _ERROR_RE.search(text)
    if m:
        return "", "MusicBrainz refused the ratings — " + m.group(1).strip()[:300]
    return text, ""


# --------------------------------------------------------------------------- #
# Config plumbing
# --------------------------------------------------------------------------- #
def _client(cfg):
    """(client_id, client_secret) as saved — both stripped, either may be ""."""
    cfg = cfg or {}
    return (str(cfg.get(CLIENT_ID_KEY) or "").strip(),
            str(cfg.get(CLIENT_SECRET_KEY) or "").strip())


def _store(cfg, **fields):
    """Persist token/identity fields through the app's OWN config writer.

    The caller's dict is updated too, so the very next `status(cfg)` in the
    same request sees what was just saved."""
    if cfg is not None:
        cfg.update(fields)
    return save_config(dict(fields))


def _tokens(body, fallback_refresh=""):
    """The config fields an OAuth2 token answer carries."""
    body = body or {}
    access = str(body.get("access_token") or "").strip()
    refresh = str(body.get("refresh_token") or "").strip() or fallback_refresh
    try:
        expires_in = float(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    return {ACCESS_KEY: access, REFRESH_KEY: refresh,
            EXPIRES_KEY: int(time.time() + expires_in)}


def _username_of(body):
    for key in _NAME_KEYS:
        got = str((body or {}).get(key) or "").strip()
        if got:
            return got
    return ""


def access_token(cfg, force=False):
    """A live access token. -> (token, refusal, where).

    "where" names the endpoint this call had to ask (the refresh endpoint), or
    "" when the saved token was still valid and no request was made. An
    unconfigured or unconnected install answers ("", "", "") without asking
    anyone."""
    cid, secret = _client(cfg)
    refresh = str((cfg or {}).get(REFRESH_KEY) or "").strip()
    # The secret is OPTIONAL: a PKCE public client registers none, and a blank
    # one must not veto a login that otherwise has everything it needs.
    if not (cid and refresh):
        return "", "", ""
    token = str((cfg or {}).get(ACCESS_KEY) or "").strip()
    try:
        expires = float((cfg or {}).get(EXPIRES_KEY) or 0)
    except (TypeError, ValueError):
        expires = 0.0
    if token and not force and expires > time.time() + _EXPIRY_SKEW:
        return token, "", ""
    where = "POST /oauth2/token (refresh_token)"
    data = {"grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": cid}
    if secret:                      # omitted ENTIRELY for a public client
        data["client_secret"] = secret
    body, err = _post_form(TOKEN_URL, data)
    fresh = _tokens(body, fallback_refresh=refresh)
    if not fresh[ACCESS_KEY]:
        return "", (err or f"{where} answered no access_token"), where
    _store(cfg, **fresh)
    return fresh[ACCESS_KEY], "", where


# --------------------------------------------------------------------------- #
# The user-facing operations
# --------------------------------------------------------------------------- #
def status(cfg=None):
    """What the settings/panel shows: is a login saved, WHO, and is it live."""
    cfg = cfg or {}
    cid, _ = _client(cfg)
    refresh = str(cfg.get(REFRESH_KEY) or "").strip()
    username = str(cfg.get(USERNAME_KEY) or "").strip()
    try:
        expires = float(cfg.get(EXPIRES_KEY) or 0)
    except (TypeError, ValueError):
        expires = 0.0
    # The secret is deliberately NOT part of "connected": a PKCE public client
    # has none, and requiring it would call a valid login disconnected.
    connected = bool(cid and refresh)
    if connected and username:
        detail = f"connected as {username}"
    elif connected:
        detail = "connected to MusicBrainz"
    elif cid:
        detail = ("the MusicBrainz client id/secret are saved, but no account "
                  "is connected yet")
    else:
        detail = "no MusicBrainz login is saved"
    out = {
        "connected": connected,
        "username": username,
        "client_id_set": bool(cid),
        "submit_enabled": bool(cfg.get(SUBMIT_KEY, True)),
        "has_access_token": bool(str(cfg.get(ACCESS_KEY) or "").strip()),
        "expires_at": int(expires),
        "detail": detail,
    }
    # How many folders the background upload queue is holding — read lazily so
    # status stays answerable even if the sync module cannot be imported.
    try:
        from server import mb_ratings_sync
        out["pending"] = int(mb_ratings_sync.state().get("pending") or 0)
    except Exception:
        out["pending"] = 0
    return out


def whoami(cfg=None):
    """Is the saved MusicBrainz login accepted? -> {ok, checked, detail}.

    `server.credential_checks` reads this shape: `checked` names the endpoint
    actually asked ("" when there was nothing to ask with, which the checker
    reports as `skipped`, never a pass) and `detail` is either the account the
    login belongs to or MusicBrainz's own refusal. Never raises."""
    cfg = cfg or {}
    cid, _ = _client(cfg)
    refresh = str(cfg.get(REFRESH_KEY) or "").strip()
    if not (cid and refresh):
        return {"ok": False, "checked": "",
                "detail": ("no MusicBrainz login is saved — connect the app to "
                           "the account whose ratings it should submit "
                           "(client id/secret and one authorization)")}
    try:
        token, err, where = access_token(cfg)
    except Exception as e:                                    # never escapes
        return {"ok": False, "checked": "POST /oauth2/token (refresh_token)",
                "detail": f"no answer ({type(e).__name__}: {e})"}
    if not token:
        return {"ok": False, "checked": where or "POST /oauth2/token",
                "detail": "MusicBrainz refused the saved login — "
                          + (err or "no access token")}
    body, err = _get_userinfo(token)
    if not body:
        return {"ok": False, "checked": "GET /oauth2/userinfo",
                "detail": "MusicBrainz refused the saved login — "
                          + (err or "no answer")}
    name = _username_of(body)
    if name and name != str(cfg.get(USERNAME_KEY) or "").strip():
        _store(cfg, **{USERNAME_KEY: name})
    return {"ok": True, "checked": "GET /oauth2/userinfo",
            "detail": f"connected as {name}" if name
                      else "the saved login is accepted by MusicBrainz"}


def connect_url(cfg=None):
    """The MusicBrainz authorize URL to open, plus the PKCE verifier to keep.

    -> ``{ok, url, verifier, detail}``. The verifier is the secret half of
    PKCE and ``state`` is the CSRF nonce the URL carries; BOTH are persisted in
    config here, so a client may finish the login with the CODE ALONE (the
    panel's own copy of the verifier is still returned, and still wins if it
    sends one). Without a saved client id there is no URL to build and `ok` is
    False with a sentence saying what to paste first."""
    cid, _ = _client(cfg)
    if not cid:
        return {"ok": False, "url": "", "verifier": "",
                "detail": ("no MusicBrainz client id is saved — register an "
                           "application at musicbrainz.org/account/applications "
                           "(redirect URI urn:ietf:wg:oauth:2.0:oob) and paste "
                           "its id and secret in Settings → Scrobbling & "
                           "submissions first")}
    verifier = secrets.token_urlsafe(64)          # 86 URL-safe chars, in PKCE's range
    state = secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    _store(cfg, **{PKCE_VERIFIER_KEY: verifier, PKCE_STATE_KEY: state})
    return {"ok": True,
            "url": AUTHORIZE_URL + "?" + urllib.parse.urlencode(params),
            "verifier": verifier,
            "detail": ("open the URL, authorize the app, and paste back the "
                       "code MusicBrainz shows")}


def _split_code(raw):
    """(code, error) from what the user handed back.

    MusicBrainz lands the browser on the redirect URI carrying either
    ``?code=…`` or ``?error=…&error_description=…``; a user may therefore paste
    the BARE code or the WHOLE URL. A refusal travels back in MusicBrainz's own
    words (preferring ``error_description``, the human sentence)."""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    if "?" not in text:
        return text, ""                       # already a bare code
    params = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    err = (params.get("error_description") or params.get("error") or [""])[0].strip()
    if err:
        return "", err
    return (params.get("code") or [""])[0].strip(), ""


def finish_connect(code, verifier, cfg=None):
    """Exchange the pasted authorization code (+ PKCE verifier) for a login.

    -> ``{ok, detail, checked, username, status}``. ``code`` may be the bare
    code OR the full redirect URL (``?code=`` is extracted; a ``?error=``
    comes back in MusicBrainz's own words). ``verifier`` is still accepted —
    a body that carries one wins — but a blank one falls back to the verifier
    ``connect_url`` saved, so the client may send nothing back. A blank client
    SECRET is legal (public PKCE client) and is then omitted from the token
    request entirely; the client id is still required. Tokens are stored
    through the app's own config writer and the account name is read from
    ``/oauth2/userinfo`` (a userinfo that fails does not fail the connect —
    the token exchange is what authorized us). A refusal is MusicBrainz's own
    text, returned, never raised."""
    cfg = cfg or {}
    cid, secret = _client(cfg)
    if not cid:
        return {"ok": False, "checked": "",
                "detail": ("no MusicBrainz client id is saved — paste it in "
                           "Settings → Scrobbling & submissions first")}
    code, oauth_error = _split_code(code)
    if oauth_error:
        return {"ok": False, "checked": "",
                "detail": "MusicBrainz refused the authorization — " + oauth_error}
    if not code:
        return {"ok": False, "checked": "", "detail": "no authorization code was given"}
    use_verifier = (str(verifier or "").strip()
                    or str(cfg.get(PKCE_VERIFIER_KEY) or "").strip())
    if not use_verifier:
        return {"ok": False, "checked": "",
                "detail": ("no PKCE verifier was given and none is saved — "
                           "start the connect again")}
    where = "POST /oauth2/token (authorization_code)"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cid,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": use_verifier,
    }
    if secret:                      # omitted ENTIRELY for a public client
        data["client_secret"] = secret
    body, err = _post_form(TOKEN_URL, data)
    fresh = _tokens(body)
    if not fresh[ACCESS_KEY]:
        return {"ok": False, "checked": where,
                "detail": "MusicBrainz refused the authorization code — "
                          + (err or "no access_token in the answer")}
    # Consumed: the single-use handshake halves go away with the code.
    _store(cfg, **fresh, **{PKCE_VERIFIER_KEY: "", PKCE_STATE_KEY: ""})
    name, checked = "", where
    got, uerr = _get_userinfo(fresh[ACCESS_KEY])
    if got:
        checked = "GET /oauth2/userinfo"
        name = _username_of(got)
        if name:
            _store(cfg, **{USERNAME_KEY: name})
    detail = f"connected as {name}" if name else "connected to MusicBrainz"
    if not got:
        detail += f" (userinfo was unavailable: {uerr})"
    return {"ok": True, "checked": checked, "detail": detail,
            "username": name, "status": status(cfg)}


def disconnect(cfg=None):
    """Forget the login — the tokens and the account name, never the app
    registration: the saved client id/secret stay, because they are the app's
    own registration and reconnecting needs them again."""
    _store(cfg, **{REFRESH_KEY: "", ACCESS_KEY: "", EXPIRES_KEY: 0,
                   USERNAME_KEY: "", PKCE_VERIFIER_KEY: "", PKCE_STATE_KEY: ""})
    return status(cfg)


# --------------------------------------------------------------------------- #
# The submission
# --------------------------------------------------------------------------- #
def snap(value):
    """A 0-100 rating on MusicBrainz's 20-point grid, HALF-UP; 0 = "do not
    send". (See the module docstring: `round` is banker's and would drop 50 to
    40 — the same half-up rule `server.ratings.from_tag` states for the same
    reason.)"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0
    num = max(0.0, min(100.0, num))
    return int((num + 10.0) // 20) * 20


def _canon(entity):
    """The canonical entity name, or "" when MusicBrainz has no such rating."""
    name = str(entity or "").strip().lower().replace("_", "-")
    return name if name in _ENTITIES else ""


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_body(entries):
    """The exact MMD-2.0 body for [(entity, mbid, value), …], all on the grid.

    Deterministic: lists are emitted in `_ENTITY_ORDER`, each entity's rows in
    the order given, so the same call always produces the same bytes."""
    buckets = {}
    for entity, mbid, value in entries:
        buckets.setdefault(_canon(entity), []).append((mbid, value))
    parts = [f'<metadata xmlns="{MMD_NS}">']
    for entity in _ENTITY_ORDER:
        rows = buckets.get(entity) or []
        if not rows:
            continue
        list_tag, item_tag = _ENTITIES[entity]
        parts.append(f"<{list_tag}>")
        for mbid, value in rows:
            parts.append(f'<{item_tag} id="{_esc(mbid)}">'
                         f"<user-rating>{int(value)}</user-rating>"
                         f"</{item_tag}>")
        parts.append(f"</{list_tag}>")
    parts.append("</metadata>")
    return "".join(parts)


def rate_one(entity, mbid, value):
    """One row's own decision — what would be sent, or why nothing is.

    -> ``{entity, mbid, value, sent, reason}``: `sent` is the snapped 0-100 (or
    None) and `reason` is a sentence for the skipped rows ("" when sent)."""
    canon = _canon(entity)
    mbid = str(mbid or "").strip().lower()
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    row = {"entity": canon or str(entity or ""), "mbid": mbid,
           "value": int(num) if num > 0 else 0, "sent": None, "reason": ""}
    if not canon:
        row["reason"] = f"MusicBrainz takes no rating for {entity!r}"
    elif not _MBID_RE.match(mbid):
        row["reason"] = "no MusicBrainz id is known for this entity"
    elif num <= 0:
        row["reason"] = "no rating of yours to send"
    else:
        snapped = snap(num)
        if snapped <= 0:
            row["reason"] = (f"{row['value']}/100 would round to 0 on "
                             "MusicBrainz's 20-point grid, and 0 deletes the "
                             "rating — skipped")
        else:
            row["sent"] = snapped
    return row


def submit_ratings(items, cfg=None):
    """Send ONE MusicBrainz rating request for ``items`` — never raises.

    ``items`` is ``[(entity, mbid, value_0_100), …]``: a whole album's
    recordings plus its release group travel in one body (that batching is the
    point — MusicBrainz takes many entities per request). Returns::

        {"ok", "checked", "detail", "sent", "skipped", "rows"}

    where `rows` is aligned to `items` (same order, same length) and each row
    says what was sent or why it was skipped; `detail` is MusicBrainz's own
    refusal when the request was refused, "" when it was accepted or when
    there was nothing to send. A login that is missing or refused marks every
    row skipped-with-reason instead of raising."""
    items = list(items or [])
    decided = [rate_one(*item) for item in items]
    entries = [(r["entity"], r["mbid"], r["sent"]) for r in decided if r["sent"]]
    if not entries:
        return {"ok": True, "checked": "", "detail": "",
                "sent": 0, "skipped": len(decided), "rows": decided}
    token, err, where = access_token(cfg)
    if not token:
        detail = "MusicBrainz refused the saved login — " + (err or "no access token")
        for row in decided:
            if row["sent"]:
                row["sent"] = None
                row["reason"] = "not sent — " + detail
        return {"ok": False, "checked": where or "POST /oauth2/token",
                "detail": detail, "sent": 0, "skipped": len(decided),
                "rows": decided}
    _, refusal = _post_rating(build_body(entries), token)
    if refusal:
        for row in decided:
            if row["sent"]:
                row["sent"] = None
                row["reason"] = "not sent — " + refusal
    sent = sum(1 for row in decided if row["sent"])
    return {"ok": not refusal, "checked": "POST /ws/2/rating",
            "detail": refusal, "sent": sent, "skipped": len(decided) - sent,
            "rows": decided}