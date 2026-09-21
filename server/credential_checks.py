"""Is each saved credential accepted? — the app's own "log in and see" surface.

`server.sources_health` answers "can this source do its job here". This module
answers the narrower question underneath it: **is the credential this install
stored actually accepted by the provider** — which a source row cannot answer.
Discogs' browse endpoint serves anonymous callers, so a discarded token left
its row green; Spotify's search is simply skipped without credentials, so a
wrong client secret left its row saying "not configured" when it was in fact
configured and refused; Last.fm states a bad key inside an ordinary body, so a
rejected key read as "this source has no tags". Every one of those is a green
or a shrug over a credential that does not work, and this is the module that
asks the provider instead.

One row per credential, each naming the config keys it reads (`needs`, the same
shape `sources_health` uses) and carrying a `check(cfg)` that calls the
provider's OWN endpoint through the provider's OWN request seam — the point is
to prove the plumbing the app already has, not a parallel copy of it. The
cheapest call each provider answers for a credential alone is the one used:

  AI        one tiny /chat/completions round trip (the key rides as a Bearer)
  Discogs   GET /oauth/identity         — "who is this token", in a header
  Last.fm   chart.gettoptags            — a chart, so nothing else has to exist
  Spotify   POST /api/token             — the grant the sources themselves need
  AcoustID  one lookup with a probe fingerprint (mlo.acoustid.verify_key)
  Soulseek  slskd's own live state + the daemon's recorded verdict
  Login     what this server's own gate would accept

Every check returns `(status, detail)` where status is `ok` | `skipped` |
`fail`, `skipped` means "there is nothing to ask with" (never a pass), and
detail is either what was verified or the provider's refusal IN ITS OWN WORDS —
"HTTP 400: {"error":"invalid_client"}", never a generic "failed".
"""

# The whole point of the AI row is that a key travels on the request; the
# prompt is deliberately one word so the check costs one token.
_AI_CHECK_SYSTEM = "You are a connectivity check. Answer with the single word: ok."
_AI_CHECK_USER = "Reply with ok."

# The credential rows, in the order the panel lists them. `needs` is what a
# person pastes for that credential AND what a `check(cfg)` cannot run without
# — with one deliberate exception, the AI key: a local LM Studio or llama.cpp
# server answers with no key at all, so requiring one would call a working
# setup broken, and the check reports whether a key was actually sent instead.
#
# `free` follows the source rows: false only where using the credential can
# cost money (an AI key can be a paid one).
CREDENTIALS = (
    {"id": "discogs", "label": "Discogs personal access token",
     "needs": ["discogs_token"], "free": True},
    {"id": "lastfm", "label": "Last.fm API key",
     "needs": ["lastfm_api_key"], "free": True},
    {"id": "spotify", "label": "Spotify client credentials",
     "needs": ["spotify_client_id", "spotify_client_secret"], "free": True},
    {"id": "acoustid", "label": "AcoustID application key",
     "needs": ["acoustid_api_key"], "free": True},
    {"id": "soulseek", "label": "Soulseek account",
     "needs": ["soulseek_username", "soulseek_password"], "free": True},
    {"id": "ai", "label": "AI provider (lyric translation)",
     "needs": ["ai_base_url", "ai_model"], "free": False},
    # Nothing to fill in: the point of this row is the ANSWER (is this server
    # asking anyone to sign in, and would the stored claim let them), so it
    # carries no `needs` and is always asked.
    {"id": "login", "label": "This server's own login",
     "needs": [], "free": True},
)


# --------------------------------------------------------------------------- #
# The providers that talk HTTP
# --------------------------------------------------------------------------- #
def _check_ai(cfg):
    from server import ai

    base, key, model = ai.ai_config(cfg)
    if not base or not model:
        return "skipped", "needs " + ", ".join(
            k for k, v in (("ai_base_url", base), ("ai_model", model)) if not v)
    try:
        reply = ai.ai_chat(cfg, _AI_CHECK_SYSTEM, _AI_CHECK_USER, timeout=20.0)
    except Exception as e:
        # `ai_chat` raises with the endpoint's own status and body already in
        # the message ("AI endpoint returned 401: …`), which is the whole
        # diagnosis — republishing it unchanged is the honest answer.
        return "fail", str(e)
    sent = "with the API key" if key else "with no API key (none is set)"
    return "ok", f'asked {model} for one word {sent}, it answered "{reply[:40]}"'


def _check_discogs(cfg):
    from server import discovery

    got = discovery.discogs_check(cfg)
    if got["ok"]:
        return "ok", got["detail"]
    if not got["checked"]:
        return "skipped", got["detail"]
    return "fail", got["detail"]


def _check_lastfm(cfg):
    from server import discovery

    got = discovery.lastfm_check(cfg)
    if got["ok"]:
        return "ok", got["detail"]
    if not got["checked"]:
        return "skipped", got["detail"]
    return "fail", got["detail"]


def _check_spotify(cfg):
    from server import integrations as intg

    got = intg.spotify_check(cfg)
    if got["ok"]:
        return "ok", got["detail"]
    if not got["checked"]:
        return "skipped", got["detail"]
    return "fail", got["detail"]


def _check_acoustid(cfg):
    from mlo import acoustid

    got = acoustid.verify_key(cfg) or {}
    code = str(got.get("code") or "")
    reason = str(got.get("reason") or "")
    if got.get("ok"):
        return "ok", ("key accepted — one lookup was answered "
                      f"({got.get('results') or 0} candidates)")
    if code == acoustid.NO_API_KEY:
        return "skipped", reason or "no acoustid_api_key is set"
    if code == acoustid.DISABLED:
        return "skipped", reason or "AcoustID is switched off in Settings"
    # `lookup_failed` and `bad_response` are the service refusing or not
    # answering; `reason` is already its own sentence.
    return "fail", reason or f"AcoustID check failed ({code})"


# --------------------------------------------------------------------------- #
# Soulseek and this server's own login — no HTTP of ours involved
# --------------------------------------------------------------------------- #
def _check_soulseek(cfg):
    """slskd signs in to the Soulseek network, so its LIVE state is the check.

    The credential itself never travels from this process: it is written into
    slskd's YAML (server/soulseek.generate_yaml) and slskd performs the
    handshake. That means the honest answer is one of three things, and the
    third is not a pass: signed in (with the account the network sees), the
    daemon's recorded refusal, or "slskd is not running, so nothing has been
    tried yet"."""
    from server import soulseek

    if not (str(cfg.get("soulseek_username") or "").strip()
            and str(cfg.get("soulseek_password") or "").strip()):
        return "skipped", "needs soulseek_username, soulseek_password"
    if not soulseek.slskd_installed():
        return "skipped", ("slskd is not installed — it is the Soulseek client "
                           "that signs in (Settings → Soulseek)")
    _ours, who, conflict = soulseek.instance_owner(cfg)
    if conflict:
        return "skipped", conflict
    if not soulseek.is_running():
        return "skipped", ("slskd is not running — press Start on the Soulseek "
                           "tab: the sign-in handshake is slskd's, and its "
                           "verdict is what this row reports")
    try:
        state = soulseek.server_state() or {}
    except Exception as e:
        # The daemon answers our port but not its own API: either it is still
        # booting (slskd re-scans the share on start) or something is wrong
        # with it. Nothing here may guess which — the log's own line, when
        # there is one, goes first.
        why = soulseek.login_error(cfg)
        return "fail", (
            f"{why} (slskd's own API did not answer: {type(e).__name__})" if why
            else f"slskd is running but its own API did not answer "
                 f"({type(e).__name__}: {e}) — it may still be starting")
    if state.get("isLoggedIn"):
        return "ok", (f"signed in to the Soulseek network as {who} "
                      if who else "signed in to the Soulseek network")
    why = soulseek.login_error(cfg)
    return "fail", (why or "slskd is running but is not signed in, and its log "
                           "says nothing about the login")


def _check_login(cfg):
    """What this server's own gate would accept.

    The password itself is only ever a hash on disk, so the check reports the
    state that decides whether a sign-in can succeed: a credential exists, in
    the users table or as the config's own claim, and its stored hash is
    readable. A hash that does not parse is NOT "wrong password" — it is a
    credential nobody can ever match, which is why it is called out here
    rather than left to a login that fails with no explanation."""
    from server import auth

    claim_name, claim_hash = auth._config_claim()
    stored = [(name, auth.user_hash(name)) for name in auth.list_users()]
    if claim_hash:
        stored.append((claim_name, claim_hash))
    stored = [(name, got) for name, got in stored if got]
    if not stored:
        warning = auth.gate_warning(cfg)
        return "skipped", ("no password is set — anyone who can reach this "
                           "server uses it" + (f" ({warning})" if warning else ""))

    def _who(name):
        # "" is the default/admin scope — the identity an install claimed
        # before the users table existed — so `repr("")` would read as a bug.
        return "the default scope" if not name else repr(name)

    broken = [(name, why) for name, got in stored
              for why in (auth.hash_problem(got),) if why]
    if broken:
        return "fail", ("the stored password hash for "
                        + ", ".join(_who(n) for n, _why in broken)
                        + f" is not readable ({broken[0][1]}) — set a new "
                        "password, no password can ever match it")
    return "ok", ("a password is set for "
                  + ", ".join(_who(name) for name, _got in stored)
                  + " — the stored hash is readable, so a sign-in can match it")


_CHECKS = {
    "ai": _check_ai,
    "discogs": _check_discogs,
    "lastfm": _check_lastfm,
    "spotify": _check_spotify,
    "acoustid": _check_acoustid,
    "soulseek": _check_soulseek,
    "login": _check_login,
}


def check(cid, cfg=None):
    """`(status, detail)` for one credential id — ok | skipped | fail.

    A `skipped` answer means there is nothing to ask with (an unset key, a
    daemon that is not running): it is never a pass, and it never reaches the
    provider."""
    fn = _CHECKS.get(str(cid or ""))
    if fn is None:
        return "skipped", "unknown credential"
    return fn(cfg or {})


def credential_ids():
    """Every credential id this module reports, in registry order."""
    return [spec["id"] for spec in CREDENTIALS]
