#!/usr/bin/env python3
"""Every credential the app stores, proved on the wire.

The app's trust boundary is its saved logins. A key that is stored under one
config name and read under another, a header built from the wrong half of a
pair, a 401 swallowed into "this source has no data" — each of those looks
exactly like a working install from the inside, and none of them is visible in
a unit test that stubs the transport. So this file runs an HTTP server and
points every provider's OWN base URL at it, then asserts on what actually
arrived: the exact stored value, under the name the provider documents.

Per credential it proves four things:

  1. SEND       the stored value reaches the wire under the right name (a
                captured request: header, query parameter or body field).
  2. MISSING    an unset key is a specific "not configured" answer — never a
                request, and never an ok.
  3. REJECTED   a refused key produces the provider's own status and words
                ("HTTP 401: You must authenticate…", "error 10: Invalid API
                key"), not a generic failure and not an empty success.
  4. NO FALSE GREEN  the HEALTH PAYLOAD the Settings page and the wizard read
                does not report a rejected credential as ok, whichever source
                happens to use the same key.

The keys that never travel over our HTTP are proved where they DO travel:
Soulseek's credentials are the `soulseek:` block of slskd's generated YAML
(slskd performs the handshake), and this server's own login is the JSON body
of POST /api/auth/login, driven through the real FastAPI app.

Runs offline and in a few seconds. The optional live pass at the end asks the
real providers, prints SKIP for every key that is not configured, and never
changes the exit code — a network outage must not fail this file.
"""
import base64
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mlo import acoustid as ac  # noqa: E402
from server import ai as ai_mod  # noqa: E402
from server import auth as auth_mod  # noqa: E402
from server import credential_checks as cc  # noqa: E402
from server import discovery  # noqa: E402
from server import integrations as intg  # noqa: E402
from server import soulseek as sk  # noqa: E402
from server import sources_health as sh  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}"
          + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# --------------------------------------------------------------------------- #
# the capture server
# --------------------------------------------------------------------------- #
class Capture:
    """An HTTP server that records every request and answers from a table.

    Routes are keyed `(METHOD, path)`; the payload is JSON-encoded unless it is
    already bytes. Nothing here knows about any provider — the providers are
    pointed at it by rewriting their own base-URL constants, so the requests
    that arrive are the ones the app really builds.
    """

    def __init__(self):
        self.requests = []
        self.routes = {}
        self.lock = threading.Lock()
        recorder = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                url = urllib.parse.urlparse(self.path)
                got = {
                    "method": method,
                    "path": url.path,
                    "query": urllib.parse.parse_qs(url.query,
                                                   keep_blank_values=True),
                    "raw_query": url.query,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": raw.decode("utf-8", "replace"),
                }
                with recorder.lock:
                    recorder.requests.append(got)
                    route = recorder.routes.get((method, url.path))
                status, payload, extra = route if route else (
                    404, {"error": "no route for %s %s" % (method, url.path)}, {})
                body = payload if isinstance(payload, bytes) \
                    else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.recorder = self
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def route(self, method, path, payload, status=200):
        with self.lock:
            self.routes[(method, path)] = (status, payload, {})
        return self

    def clear(self):
        with self.lock:
            self.requests.clear()
            self.routes.clear()

    def sent(self, method=None, path=None):
        """Every captured request matching *method*/*path*, oldest first."""
        with self.lock:
            rows = list(self.requests)
        return [r for r in rows
                if (method is None or r["method"] == method)
                and (path is None or r["path"] == path)]

    def one(self, method, path):
        got = self.sent(method, path)
        if len(got) != 1:
            raise AssertionError(
                f"expected exactly one {method} {path}, got {len(got)}: "
                f"{[(r['method'], r['path']) for r in self.sent()]}")
        return got[0]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


cap = Capture()

# Point every provider at the capture server. These ARE the module constants
# the request builders interpolate, so no request-building code is bypassed.
def point_at_providers():
    discovery.DISCOGS_BASE = cap.base
    discovery.LASTFM_BASE = cap.base + "/2.0/"
    discovery.SPOTIFY_API = cap.base + "/v1"
    intg._SPOTIFY_TOKEN_URL = cap.base + "/api/token"
    intg._SPOTIFY_SEARCH = cap.base + "/v1/search"
    ac.API_URL = cap.base + "/v2/lookup"
    ac.SUBMIT_API_URL = cap.base + "/v2/submit"
    # Provider etiquette intervals (1 req/s for the crowdsourced APIs) would
    # only make this file slow; nothing here is about being polite.
    discovery._HOST_WAIT.clear()
    discovery._CACHE.clear()
    discovery._LAST_HTTP.clear()
    intg._ADVISORY_CACHE.clear()


point_at_providers()
print(f"capture server on {cap.base}")


# --------------------------------------------------------------------------- #
# 1. AI — ai_api_key (+ base_url/model)
# --------------------------------------------------------------------------- #
print("== AI provider: ai_api_key ==")
AI_CFG = {"ai_base_url": cap.base + "/v1", "ai_model": "gpt-test",
          "ai_api_key": "sk-stored-value-123", "ai_effort": "minimal"}
AI_OK = {"choices": [{"message": {"content": "ok"}}]}

cap.clear()
cap.route("POST", "/v1/chat/completions", AI_OK)
reply = ai_mod.ai_chat(AI_CFG, "sys", "user")
req = cap.one("POST", "/v1/chat/completions")
check("ai: the key travels as a Bearer token",
      req["headers"].get("authorization") == "Bearer sk-stored-value-123",
      req["headers"].get("authorization"))
check("ai: the stored value is exact, not truncated or re-quoted",
      req["headers"].get("authorization", "").split(" ", 1)[-1]
      == AI_CFG["ai_api_key"])
check("ai: the model and prompt go in the JSON body",
      json.loads(req["body"])["model"] == "gpt-test"
      and json.loads(req["body"])["messages"][-1]["content"] == "user")
check("ai: the call answered", reply == "ok", reply)

# A local server (LM Studio, llama.cpp) ignores the key entirely and works
# with none, so an empty ai_api_key must send NO Authorization header.
cap.clear()
cap.route("POST", "/v1/chat/completions", AI_OK)
ai_mod.ai_chat({"ai_base_url": cap.base + "/v1", "ai_model": "local"},
               "sys", "user")
check("ai: no key set sends no Authorization header at all",
      "authorization" not in cap.one("POST", "/v1/chat/completions")["headers"])
check("ai: a keyless local server is still 'configured'",
      ai_mod.ai_configured({"ai_base_url": cap.base + "/v1",
                            "ai_model": "local"}) is True
      and cc.check("ai", {"ai_base_url": cap.base + "/v1",
                          "ai_model": "local"})[0] == "ok")

cap.clear()
cap.route("POST", "/v1/chat/completions",
          {"error": {"message": "Incorrect API key provided",
                     "type": "invalid_request_error"}}, status=401)
try:
    ai_mod.ai_chat(AI_CFG, "sys", "user")
    check("ai: a 401 raises instead of returning an empty answer", False,
          "ai_chat returned normally")
except ValueError as e:
    text = str(e)
    check("ai: a rejected key reports the status and the provider's own words",
          "401" in text and "Incorrect API key provided" in text, text)
st, detail = cc.check("ai", AI_CFG)
check("ai: the credential row for a rejected key is a fail, with the reason",
      st == "fail" and "401" in detail and "Incorrect API key provided" in detail,
      f"{st}: {detail}")

st, detail = cc.check("ai", {})
check("ai: no base URL/model is a 'not configured' answer, not a request",
      st == "skipped" and "ai_base_url" in detail and "ai_model" in detail,
      f"{st}: {detail}")


# --------------------------------------------------------------------------- #
# 2. Discogs — discogs_token
# --------------------------------------------------------------------------- #
print("== Discogs: discogs_token ==")
DISCOGS_CFG = {"discogs_token": "dc-stored-token-456"}
DC = "Discogs token=dc-stored-token-456"

cap.clear()
cap.route("GET", "/oauth/identity", {"id": 7, "username": "spinnin-records"})
got = discovery.discogs_check(DISCOGS_CFG)
req = cap.one("GET", "/oauth/identity")
check("discogs: the token travels in the Authorization header, Discogs' own "
      "name for it", req["headers"].get("authorization") == DC,
      req["headers"].get("authorization"))
check("discogs: the identity endpoint is the one asked",
      got["ok"] is True and got["checked"] == "GET /oauth/identity"
      and "spinnin-records" in got["detail"]
      # …and the row's own sentence names what was asked, not only the answer
      and "GET /oauth/identity" in got["detail"], str(got))
check("discogs: a token is not also leaked into the query string",
      "token" not in req["query"])

# The data endpoints are a DIFFERENT form of the same credential (Discogs
# documents `?token=` for them and the header for /oauth/identity): both must
# carry the stored value, and the app must not mix them up.
cap.clear()
cap.route("GET", "/database/search", {"results": [{"id": 42}]})
cap.route("GET", "/releases/42", {"genres": ["Rock"], "styles": ["Shoegaze"]})
discovery.invalidate()
detail = discovery.discogs_album_genres("Radiohead", "Pablo Honey",
                                        DISCOGS_CFG)
check("discogs: the release search carries ?token=<stored>",
      cap.one("GET", "/database/search")["query"].get("token")
      == ["dc-stored-token-456"])
check("discogs: the release detail carries it too",
      cap.one("GET", "/releases/42")["query"].get("token")
      == ["dc-stored-token-456"])
check("discogs: and the genres came back", detail == ["Rock", "Shoegaze"], detail)

cap.clear()
got = discovery.discogs_check({})
check("discogs: no token is a 'not configured' answer and sends nothing",
      got["ok"] is False and got["checked"] == ""
      and "no discogs_token" in got["detail"] and not cap.sent(), str(got))
check("discogs: the credential row says skipped, naming the key",
      cc.check("discogs", {}) == ("skipped", "no discogs_token is set"),
      str(cc.check("discogs", {})))

cap.clear()
cap.route("GET", "/oauth/identity",
          {"message": "You must authenticate to access this resource."},
          status=401)
got = discovery.discogs_check(DISCOGS_CFG)
check("discogs: a rejected token reports HTTP 401 and Discogs' own sentence",
      got["ok"] is False and "401" in got["detail"]
      and "You must authenticate" in got["detail"], str(got))
st, detail = cc.check("discogs", DISCOGS_CFG)
check("discogs: the credential row for a rejected token is a fail",
      st == "fail" and "401" in detail, f"{st}: {detail}")

# The fix that matters most here: the SOURCE row used to read "no Discogs
# genres" for a refused token, because the search endpoint answers anonymously
# and a 401 is just another empty answer.
cap.clear()
cap.route("GET", "/database/search",
          {"message": "You must authenticate to access this resource."},
          status=401)
discovery.invalidate()
st, detail = sh._probe_genre("discogs", DISCOGS_CFG)
check("discogs: a refused token is not reported as 'this source has no genres'",
      st == "fail" and "401" in detail and "authenticate" in detail,
      f"{st}: {detail}")


# --------------------------------------------------------------------------- #
# 3. Last.fm — lastfm_api_key
# --------------------------------------------------------------------------- #
print("== Last.fm: lastfm_api_key ==")
LASTFM_CFG = {"lastfm_api_key": "lf-stored-key-789"}

cap.clear()
cap.route("GET", "/2.0/", {"tags": {"tag": [{"name": "rock"}]}})
got = discovery.lastfm_check(LASTFM_CFG)
req = cap.one("GET", "/2.0/")
check("lastfm: the key travels as ?api_key=<stored>",
      req["query"].get("api_key") == ["lf-stored-key-789"], req["raw_query"])
check("lastfm: the method and the JSON envelope Last.fm requires are sent",
      req["query"].get("method") == ["chart.gettoptags"]
      and req["query"].get("format") == ["json"], req["raw_query"])
check("lastfm: an accepted key is ok with what was asked",
      got["ok"] is True and got["checked"] == "chart.gettoptags"
      and "chart.gettoptags" in got["detail"], str(got))

cap.clear()
cap.route("GET", "/2.0/", {"album": {"tags": {"tag": [{"name": "shoegaze"}]}}})
tags = discovery.lastfm_album_genres("Ride", "Nowhere", LASTFM_CFG)
check("lastfm: the genre readers carry the same key",
      cap.one("GET", "/2.0/")["query"].get("api_key") == ["lf-stored-key-789"])
check("lastfm: and got the tags back", tags == ["shoegaze"], str(tags))

cap.clear()
got = discovery.lastfm_check({})
check("lastfm: no key is a 'not configured' answer and sends nothing",
      got["ok"] is False and got["checked"] == ""
      and "no lastfm_api_key" in got["detail"] and not cap.sent(), str(got))

# Last.fm states a refused key INSIDE an ordinary body ("error 10: Invalid API
# key"), which used to be swallowed into "no Last.fm tags" — a green-looking
# source for a key that was never accepted.
cap.clear()
LASTFM_REFUSAL = {"error": 10,
                   "message": "Invalid API key - You must be granted a valid "
                              "key."}
cap.route("GET", "/2.0/", dict(LASTFM_REFUSAL, links=[]), status=403)
got = discovery.lastfm_check(LASTFM_CFG)
check("lastfm: a rejected key reports Last.fm's own error code and message",
      got["ok"] is False and "10" in got["detail"]
      and "Invalid API key" in got["detail"], str(got))
st, detail = cc.check("lastfm", LASTFM_CFG)
check("lastfm: the credential row for a rejected key is a fail",
      st == "fail" and "Invalid API key" in detail, f"{st}: {detail}")
cap.clear()
cap.route("GET", "/2.0/", LASTFM_REFUSAL)
st, detail = sh._probe_genre("lastfm", LASTFM_CFG)
check("lastfm: the genre probe no longer reads as 'no Last.fm tags'",
      st == "fail" and "Invalid API key" in detail, f"{st}: {detail}")
check("lastfm: the Discover probe says the same thing",
      "Invalid API key" in sh._probe_discover("lastfm", LASTFM_CFG)[1],
      sh._probe_discover("lastfm", LASTFM_CFG)[1])


# --------------------------------------------------------------------------- #
# 4. Spotify — spotify_client_id + spotify_client_secret
# --------------------------------------------------------------------------- #
print("== Spotify: spotify_client_id + spotify_client_secret ==")
SP_CFG = {"spotify_client_id": "cid-stored", "spotify_client_secret": "sec-stored"}
SP_BASIC = "Basic " + base64.b64encode(b"cid-stored:sec-stored").decode("ascii")


def spotify_happy():
    cap.clear()
    cap.route("POST", "/api/token",
              {"access_token": "sp-token-abc", "expires_in": 3600})
    cap.route("GET", "/v1/search", {"tracks": {"items": []}})


spotify_happy()
got = intg.spotify_check(SP_CFG)
req = cap.one("POST", "/api/token")
check("spotify: both halves travel together as HTTP Basic",
      req["headers"].get("authorization") == SP_BASIC,
      req["headers"].get("authorization"))
check("spotify: the grant is the client-credentials one",
      req["body"] == "grant_type=client_credentials", req["body"])
check("spotify: the check reports what it asked",
      got["ok"] is True and "client_credentials" in got["checked"]
      and "/api/token" in got["detail"], str(got))

spotify_happy()
intg._SPOTIFY_TOKEN.clear()
check("spotify: a token is issued", intg._spotify_token(SP_CFG) == "sp-token-abc")
cap.clear()
cap.route("GET", "/v1/search", {"tracks": {"items": [
    {"explicit": True, "external_ids": {"isrc": "GBAYE9200070"}}]}})
hit = intg._spotify_advisory("GBAYE9200070", SP_CFG)
req = cap.one("GET", "/v1/search")
check("spotify: the access token rides on the search as a Bearer",
      req["headers"].get("authorization") == "Bearer sp-token-abc",
      req["headers"].get("authorization"))
check("spotify: and the search answered", hit == (1, "spotify-isrc"), str(hit))

cap.clear()
got = intg.spotify_check({"spotify_client_id": "cid-stored"})
check("spotify: half a pair is 'not configured' and sends nothing",
      got["ok"] is False and got["checked"] == ""
      and "spotify_client_id/spotify_client_secret" in got["detail"]
      and not cap.sent(), str(got))
check("spotify: the credential row names both keys",
      cc.check("spotify", {}) ==
      ("skipped", "no spotify_client_id/spotify_client_secret is set"),
      str(cc.check("spotify", {})))

# A rejected client used to be indistinguishable from an unreachable host:
# `_advisory_post` threw the 400 body away and `_spotify_token` returned None,
# so the source was silently "not configured" while it WAS configured.
cap.clear()
cap.route("POST", "/api/token",
          {"error": "invalid_client",
           "error_description": "Invalid client secret"}, status=400)
got = intg.spotify_check(SP_CFG)
check("spotify: a rejected client reports the OAuth error body verbatim",
      got["ok"] is False and "400" in got["detail"]
      and "invalid_client" in got["detail"], str(got))
st, detail = cc.check("spotify", SP_CFG)
check("spotify: the credential row for a rejected client is a fail",
      st == "fail" and "invalid_client" in detail, f"{st}: {detail}")
cap.clear()
cap.route("POST", "/api/token", {"error": "invalid_client"}, status=400)
intg._SPOTIFY_TOKEN.clear()
check("spotify: the token seam still answers None for the sources",
      intg._spotify_token(SP_CFG) is None)
check("spotify: and the refusal is readable afterwards",
      "invalid_client" in intg.spotify_last_error(), intg.spotify_last_error())
st, detail = sh._probe_advisory("spotify-isrc", SP_CFG)
check("spotify: the advisory probe reports the refusal, not 'check the credentials'",
      st == "fail" and "invalid_client" in detail, f"{st}: {detail}")


# --------------------------------------------------------------------------- #
# 5. AcoustID — acoustid_api_key
# --------------------------------------------------------------------------- #
print("== AcoustID: acoustid_api_key ==")
AC_CFG = {"acoustid_enabled": True, "acoustid_api_key": "ac-stored-key-321"}

cap.clear()
cap.route("POST", "/v2/lookup", {"status": "ok", "results": []})
got = ac.verify_key(AC_CFG)
req = cap.one("POST", "/v2/lookup")
form = urllib.parse.parse_qs(req["body"])
check("acoustid: the key travels as the `client` form field, in the POST body",
      form.get("client") == ["ac-stored-key-321"], req["body"][:120])
check("acoustid: the request carries a fingerprint and a duration",
      bool(form.get("fingerprint", [""])[0]) and bool(form.get("duration")),
      req["body"][:120])
check("acoustid: an accepted key is ok, a no-match being a fine answer",
      got["ok"] is True and got["results"] == 0 and got["code"] == ac.OK,
      str(got))
st, detail = cc.check("acoustid", AC_CFG)
check("acoustid: the credential row reports the accepted key",
      st == "ok" and "accepted" in detail, f"{st}: {detail}")

cap.clear()
got = ac.verify_key({"acoustid_enabled": True})
check("acoustid: no key is 'not configured' and sends nothing",
      got["ok"] is False and got["code"] == ac.NO_API_KEY and not cap.sent(),
      str(got))
check("acoustid: the credential row says skipped with the module's own note",
      cc.check("acoustid", {"acoustid_enabled": True, "acoustid_api_key": ""})[0]
      == "skipped",
      str(cc.check("acoustid", {"acoustid_enabled": True, "acoustid_api_key": ""})))

cap.clear()
cap.route("POST", "/v2/lookup",
          {"status": "error", "error": {"code": 4, "message": "invalid API key"}},
          status=400)
got = ac.verify_key(AC_CFG)
check("acoustid: a rejected key reports the service's sentence verbatim",
      got["ok"] is False and got["code"] == ac.LOOKUP_FAILED
      and "HTTP 400" in got["reason"] and "invalid API key" in got["reason"],
      str(got))
st, detail = cc.check("acoustid", AC_CFG)
check("acoustid: the credential row for a rejected key is a fail",
      st == "fail" and "invalid API key" in detail, f"{st}: {detail}")


# --------------------------------------------------------------------------- #
# 5b. AcoustID user key — acoustid_user_key (submissions)
# --------------------------------------------------------------------------- #
# A DIFFERENT credential: the application key can only look up, and only a
# submission proves the user key, so the wire that has to carry it is
# /v2/submit (client + user as separate fields).
print("== AcoustID: acoustid_user_key ==")
AU_CFG = {"acoustid_enabled": True, "acoustid_api_key": "ac-stored-key-321",
          "acoustid_user_key": "ac-user-key-654"}
SUBMITTED = {"status": "ok",
             "submissions": [{"index": 0, "id": 12345, "status": "pending"}]}

cap.clear()
cap.route("POST", "/v2/submit", SUBMITTED)
got = ac.verify_user_key(AU_CFG)
req = cap.one("POST", "/v2/submit")
form = urllib.parse.parse_qs(req["body"])
check("acoustid-user: the USER key travels as the `user` form field, beside the "
      "application key as `client`",
      form.get("user") == ["ac-user-key-654"]
      and form.get("client") == ["ac-stored-key-321"], req["body"][:120])
check("acoustid-user: the probe is one fingerprint-only submission (source 3)",
      bool(form.get("fingerprint.0", [""])[0]) and form.get("source.0") == ["3"]
      and not any(k.startswith("mbid") for k in form), req["body"][:120])
check("acoustid-user: an accepted key is ok, the submission being its proof",
      got["ok"] is True and got["id"] == 12345 and got["status"] == "pending",
      str(got))
st, detail = cc.check("acoustid-user", AU_CFG)
check("acoustid-user: the credential row reports the accepted user key",
      st == "ok" and "accepted" in detail, f"{st}: {detail}")

cap.clear()
got = ac.verify_user_key({"acoustid_enabled": True,
                          "acoustid_api_key": "ac-stored-key-321"})
check("acoustid-user: no user key is 'not configured' and sends nothing",
      got["ok"] is False and got["code"] == ac.NO_USER_KEY and not cap.sent(),
      str(got))
check("acoustid-user: the credential row says skipped with the module's own note",
      cc.check("acoustid-user", {"acoustid_enabled": True,
                                 "acoustid_api_key": "k"})[0] == "skipped",
      str(cc.check("acoustid-user", {"acoustid_enabled": True,
                                     "acoustid_api_key": "k"})))

cap.clear()
cap.route("POST", "/v2/submit",
          {"status": "error",
           "error": {"code": 8, "message": "invalid user API key"}},
          status=400)
got = ac.verify_user_key(AU_CFG)
check("acoustid-user: a refused user key reports the service's sentence verbatim",
      got["ok"] is False and got["code"] == ac.LOOKUP_FAILED
      and "HTTP 400" in got["reason"] and "invalid user API key" in got["reason"],
      str(got))
st, detail = cc.check("acoustid-user", AU_CFG)
check("acoustid-user: the credential row for a refused key is a fail",
      st == "fail" and "invalid user API key" in detail, f"{st}: {detail}")


# --------------------------------------------------------------------------- #
# 6. Soulseek — soulseek_username + soulseek_password
# --------------------------------------------------------------------------- #
print("== Soulseek: soulseek_username + soulseek_password ==")
# The credential does not travel over HTTP: it is written into slskd's YAML
# and slskd performs the network handshake. The YAML is therefore the wire.
SK_CFG = {"soulseek_username": "night-owl", "soulseek_password": "p@ss:with#chars",
          "music_folder": tempfile.mkdtemp(prefix="mlo-slsk-")}
text, api_key = sk.generate_yaml(SK_CFG)
lines = [ln.rstrip() for ln in text.splitlines()]


def _section_of(all_lines, name):
    """The indented block under a top-level `name:` key."""
    out, inside = [], False
    for line in all_lines:
        line = line.rstrip()
        if line.startswith(f"{name}:"):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            break
        if inside:
            out.append(line)
    return out


soulseek_block = _section_of(lines, "soulseek")
check("soulseek: the username is written into slskd's `soulseek:` block",
      '  username: "night-owl"' in soulseek_block, str(soulseek_block))
check("soulseek: the password goes with it, quoted as slskd needs",
      '  password: "p@ss:with#chars"' in soulseek_block, str(soulseek_block))
check("soulseek: the two halves are sent together, never one without the other",
      any(ln.strip().startswith("username:") for ln in soulseek_block)
      and any(ln.strip().startswith("password:") for ln in soulseek_block))
empty_text, _key = sk.generate_yaml({"music_folder": SK_CFG["music_folder"]})
empty_block = _section_of(empty_text.splitlines(), "soulseek")
check("soulseek: with no credentials, slskd gets empty strings, not a guess",
      '  username: ""' in empty_block and '  password: ""' in empty_block,
      str(empty_block))

check("soulseek: an empty pair is 'not configured', never a pass",
      cc.check("soulseek", {})[0] == "skipped"
      and cc.check("soulseek", {})[1] == "needs soulseek_username, soulseek_password",
      str(cc.check("soulseek", {})))
check("soulseek: a username without a password is still not configured",
      cc.check("soulseek", {"soulseek_username": "night-owl"})[0] == "skipped",
      str(cc.check("soulseek", {"soulseek_username": "night-owl"})))

# The live verdict is slskd's: it is the process that signs in. Stub the daemon
# rather than spawn it — the point here is what the ROW says, not slskd itself.
_saved = (sk.slskd_installed, sk.instance_owner, sk.is_running, sk.server_state,
          sk.login_error)
try:
    sk.slskd_installed = lambda: True
    sk.instance_owner = lambda cfg=None: (True, "night-owl", "")
    sk.is_running = lambda: True
    sk.server_state = lambda: {"isLoggedIn": True}
    sk.login_error = lambda cfg=None: ""
    st, detail = cc.check("soulseek", SK_CFG)
    check("soulseek: a signed-in daemon is ok, naming the live account",
          st == "ok" and "night-owl" in detail, f"{st}: {detail}")

    sk.server_state = lambda: {"isLoggedIn": False}
    sk.login_error = lambda cfg=None: ("[ERR] Login failed: INVALIDPASS - check "
                                       "your Soulseek username and password")
    st, detail = cc.check("soulseek", SK_CFG)
    check("soulseek: a refused login republishes the daemon's own sentence",
          st == "fail" and "INVALIDPASS" in detail, f"{st}: {detail}")

    sk.is_running = lambda: False
    st, detail = cc.check("soulseek", SK_CFG)
    check("soulseek: a daemon that is not running says so instead of passing",
          st == "skipped" and "not running" in detail, f"{st}: {detail}")

    sk.is_running = lambda: True
    sk.instance_owner = lambda cfg=None: (False, "someone-else",
                                          "another application's slskd is "
                                          "already using port 5030 (signed in "
                                          "as someone-else)")
    st, detail = cc.check("soulseek", SK_CFG)
    check("soulseek: another app's slskd is reported as that, not as our failure",
          st == "skipped" and "someone-else" in detail, f"{st}: {detail}")
finally:
    (sk.slskd_installed, sk.instance_owner, sk.is_running, sk.server_state,
     sk.login_error) = _saved


# --------------------------------------------------------------------------- #
# 7. This server's own login — auth_username + the stored password
# --------------------------------------------------------------------------- #
print("== this server's own login ==")
auth_tmp = tempfile.mkdtemp(prefix="mlo-api-logins-")
auth_mod.db_path = lambda: os.path.join(auth_tmp, "auth.db")
auth_mod._initialized = False
auth_mod.revoke_all()

PASSWORD = "a-stored-password-1"
STORED = auth_mod.hash_password(PASSWORD)
check("login: the stored credential is a hash, never the password",
      PASSWORD not in STORED and STORED.startswith("pbkdf2$"), STORED[:24])
check("login: the exact stored password verifies against the stored hash",
      auth_mod.verify_password(PASSWORD, STORED) is True)
check("login: a near miss does not",
      auth_mod.verify_password(PASSWORD[:-1], STORED) is False)

_saved_claim = auth_mod._config_claim
auth_mod._config_claim = lambda: ("", STORED)
try:
    st, detail = cc.check("login", {"server_host": "127.0.0.1", "auth_mode": "auto"})
    check("login: a readable stored hash is ok, and says whose it is",
          st == "ok" and "default scope" in detail, f"{st}: {detail}")

    auth_mod._config_claim = lambda: ("owner", "not-a-hash-at-all")
    st, detail = cc.check("login", {"server_host": "127.0.0.1", "auth_mode": "auto"})
    check("login: a corrupt hash is a fail, not a silent lockout",
          st == "fail" and "not readable" in detail and "owner" in detail,
          f"{st}: {detail}")

    auth_mod._config_claim = lambda: ("", "")
    st, detail = cc.check("login", {"server_host": "0.0.0.0", "auth_mode": "auto",
                                    "auth_password_hash": ""})
    check("login: no password is 'nothing is asked of anyone', never an ok",
          st == "skipped" and "no password is set" in detail, f"{st}: {detail}")
    check("login: …and it names the misconfiguration when the server is exposed",
          "loopback" in detail or "network" in detail, detail)
finally:
    auth_mod._config_claim = _saved_claim

# The password's real wire form: the JSON body of POST /api/auth/login, driven
# through the actual FastAPI app (the full gate suite lives in test_auth.py —
# this is the credential's own end-to-end proof).
try:
    from fastapi.testclient import TestClient
    import mlo.config as config_mod
    from server import main as main_mod

    check("login: hash_problem accepts a real hash",
          auth_mod.hash_problem(STORED) == "")
    check("login: hash_problem explains an unparsable one",
          "pbkdf2" in auth_mod.hash_problem("hunter2"),
          auth_mod.hash_problem("hunter2"))

    _real_load = config_mod.load_config
    config_mod.load_config = lambda *a, **k: {
        "auth_password_hash": STORED, "auth_username": "", "server_host": "0.0.0.0",
        "auth_mode": "auto", "auth_session_days": 30}
    _gate = {"required": True, "has_password": True, "username": "",
             "host": "0.0.0.0", "public_url": "", "session_days": 30, "mode": "auto"}
    _saved_gate = (auth_mod.cached_state, auth_mod.current_state)
    auth_mod.cached_state = lambda: dict(_gate)
    auth_mod.current_state = lambda refresh=False: dict(_gate)
    try:
        client = TestClient(main_mod.app)
        r = client.post("/api/auth/login", json={"password": PASSWORD})
        check("login: the stored password in the `password` field opens a session",
              r.status_code == 200 and bool((r.json() or {}).get("token")),
              f"{r.status_code} {r.text[:120]}")
        check("login: and the session cookie is set for the media paths",
              "mlo_session" in r.cookies)
        r = client.post("/api/auth/login", json={"password": "wrong-password-9"})
        check("login: a wrong password is a 401 that says so",
              r.status_code == 401 and "wrong password" in r.text.lower(),
              f"{r.status_code} {r.text[:120]}")
        r = client.post("/api/auth/login", json={"pass": PASSWORD})
        check("login: the field really is named `password` (a rename is refused)",
              r.status_code == 422, f"{r.status_code} {r.text[:120]}")
    finally:
        config_mod.load_config = _real_load
        auth_mod.cached_state, auth_mod.current_state = _saved_gate
except ImportError as e:  # pragma: no cover
    print(f"  SKIP  TestClient unavailable: {e}")


# --------------------------------------------------------------------------- #
# 8. the route the Settings page and the setup wizard actually call
# --------------------------------------------------------------------------- #
print("== /api/sources/health surfaces the credential kind ==")
# Every credential configured, every one of them REFUSED by the capture server
# below — the configuration a stale or mistyped key produces in the wild.
BAD_CFG = {
    "discogs_token": "dc-stored-token-456",
    "lastfm_api_key": "lf-stored-key-789",
    "spotify_client_id": "cid-stored", "spotify_client_secret": "sec-stored",
    "acoustid_enabled": True, "acoustid_api_key": "ac-stored-key-321",
    "acoustid_user_key": "ac-user-key-654",
    "ai_base_url": cap.base + "/v1", "ai_model": "gpt-test",
    "ai_api_key": "sk-stored-value-123", "ai_effort": "minimal",
    "soulseek_username": "night-owl", "soulseek_password": "pw",
}
try:
    from fastapi.testclient import TestClient
    from server import main as main_mod

    _real_main_load = main_mod.load_config
    _saved_login_check = cc._CHECKS["login"]
    cc._CHECKS["login"] = lambda cfg: ("ok", "a password is set")
    main_mod.load_config = lambda *a, **k: dict(BAD_CFG)
    # The gate answers for THIS request from the cached state; the same patch
    # test_auth.py uses, so this section does not depend on the bind address
    # the developer's own config.json happens to hold.
    _open_gate = {"required": False, "has_password": False, "username": "",
                  "host": "127.0.0.1", "public_url": "", "session_days": 30,
                  "mode": "auto"}
    _saved_gate = (auth_mod.cached_state, auth_mod.current_state)
    auth_mod.cached_state = lambda: dict(_open_gate)
    auth_mod.current_state = lambda refresh=False: dict(_open_gate)
    try:
        client = TestClient(main_mod.app)
        r = client.get("/api/sources/health?kind=credentials&probe=0")
        body = r.json() if r.status_code == 200 else {}
        check("route: kind=credentials answers every credential row",
              r.status_code == 200
              and [s["id"] for s in body.get("sources", [])] == cc.credential_ids(),
              f"{r.status_code} {[s['id'] for s in body.get('sources', [])]}")
        check("route: probe=0 reports the config and probes nothing",
              body.get("sources")
              and all(s["status"] == "ok" and s["detail"] == "configured"
                      for s in body["sources"]),
              str([(s["id"], s["status"], s["detail"])
                   for s in body.get("sources", [])]))
        cap.clear()
        cap.route("GET", "/oauth/identity", {"message": "token rejected"}, status=401)
        discovery._LAST_HTTP.clear()
        r = client.get("/api/sources/health/discogs?kind=credentials&probe=1")
        row = r.json() if r.status_code == 200 else {}
        check("route: one credential's row is bare, probed and not green",
              r.status_code == 200 and row.get("kind") == "credentials"
              and row.get("status") == "fail" and "401" in row.get("detail", ""),
              f"{r.status_code} {row}")
        check("route: the id that is ALSO a genre source resolves by kind",
              client.get("/api/sources/health/discogs?kind=genre&probe=0")
              .json().get("kind") == "genre")
        check("route: an unknown kind is a 400 naming the kinds",
              client.get("/api/sources/health?kind=nope").status_code == 400)
        check("route: an unknown credential id is a 404",
              client.get("/api/sources/health/nope?kind=credentials")
              .status_code == 404)
    finally:
        main_mod.load_config = _real_main_load
        cc._CHECKS["login"] = _saved_login_check
        auth_mod.cached_state, auth_mod.current_state = _saved_gate
except ImportError as e:  # pragma: no cover
    print(f"  SKIP  TestClient unavailable: {e}")


# --------------------------------------------------------------------------- #
# 9. the health payload the UI reads must never go green on a bad key
# --------------------------------------------------------------------------- #
print("== the health payload reports a rejected credential as a failure ==")
# The login row reads this machine's real config, which is not what this
# section is about; the row's own behaviour is proved above.
_saved_login_check = cc._CHECKS["login"]
cc._CHECKS["login"] = lambda cfg: ("skipped", "not under test here")

try:
    cap.clear()
    cap.route("GET", "/oauth/identity", {"message": "token rejected"}, status=401)
    cap.route("GET", "/2.0/", {"error": 10, "message": "Invalid API key"})
    cap.route("POST", "/api/token", {"error": "invalid_client"}, status=400)
    cap.route("POST", "/v2/lookup",
              {"status": "error", "error": {"code": 4, "message": "invalid API key"}},
              status=400)
    cap.route("POST", "/v2/submit",
              {"status": "error",
               "error": {"code": 8, "message": "invalid user API key"}},
              status=400)
    cap.route("POST", "/v1/chat/completions", {"error": {"message": "bad key"}},
              status=401)
    intg._SPOTIFY_TOKEN.clear()
    discovery._CACHE.clear()
    discovery._LAST_HTTP.clear()
    discovery.invalidate()

    rows = {r["id"]: r for r in
            sh.health_payload(BAD_CFG, kind="credentials", probe=True)["sources"]}
    for cid in ("discogs", "lastfm", "spotify", "acoustid", "acoustid-user", "ai"):
        row = rows[cid]
        check(f"payload: a rejected {cid} credential is never an ok row",
              row["status"] == "fail", f"{row['status']}: {row['detail']}")
        check(f"payload: the {cid} row explains itself in the provider's words",
              bool(row["detail"].strip()) and row["detail"] != "configured",
              row["detail"])
    # Soulseek's handshake is slskd's, so the row can only ever report that
    # daemon's state. With nothing signed in the honest answers are "not
    # running" or the daemon's refusal — never ok (the ok/fail branches are
    # proved above with the daemon stubbed).
    check("payload: a soulseek row never goes green on an unverified login",
          rows["soulseek"]["status"] in ("skipped", "fail")
          and rows["soulseek"]["status"] != "ok"
          and bool(rows["soulseek"]["detail"].strip()),
          f"{rows['soulseek']['status']}: {rows['soulseek']['detail']}")
    check("payload: the login row is unaffected by the others",
          rows["login"]["status"] == "skipped", str(rows["login"]))
finally:
    cc._CHECKS["login"] = _saved_login_check

# And the mirror image: with no keys at all, every keyed credential row is a
# 'needs <key>' skip — a fresh install must not read as a broken one.
cap.clear()
rows = {r["id"]: r for r in
        sh.health_payload({}, kind="credentials", probe=True)["sources"]}
check("payload: an unconfigured install is skipped, not failed",
      all(rows[cid]["status"] == "skipped"
          for cid in ("discogs", "lastfm", "spotify", "acoustid",
                      "acoustid-user", "soulseek", "ai")),
      str({k: v["status"] for k, v in rows.items()}))
check("payload: and each says which key it wants",
      all(rows[cid]["detail"].startswith("needs ")
          for cid in ("discogs", "lastfm", "spotify", "acoustid",
                      "acoustid-user", "soulseek", "ai")),
      str({k: v["detail"] for k, v in rows.items()}))
check("payload: probing an unconfigured credential sends no request at all",
      not cap.sent(), str([(r["method"], r["path"]) for r in cap.sent()]))


# --------------------------------------------------------------------------- #
# the optional live pass — informational, never part of the exit code
# --------------------------------------------------------------------------- #
print("== live pass (informational only; SKIP means no key is configured) ==")


def _live(label, keys, run):
    try:
        from mlo.config import load_config
        cfg = load_config() or {}
    except Exception as e:
        print(f"  SKIP  {label}: the app config is unreadable ({e})")
        return
    missing = [k for k in keys if not str(cfg.get(k) or "").strip()]
    if missing:
        print(f"  SKIP  {label}: no {'/'.join(missing)} in this install's config")
        return
    try:
        st, detail = run(cfg)
    except Exception as e:
        print(f"  SKIP  {label}: could not be reached ({type(e).__name__}: {e})")
        return
    verdict = {"ok": "LIVE ok", "skipped": "SKIP"}.get(st, "LIVE fail")
    print(f"  {verdict:9} {label}: {detail}")


_live("discogs", ["discogs_token"], lambda cfg: cc.check("discogs", cfg))
_live("lastfm", ["lastfm_api_key"], lambda cfg: cc.check("lastfm", cfg))
_live("spotify", ["spotify_client_id", "spotify_client_secret"],
      lambda cfg: cc.check("spotify", cfg))
_live("acoustid", ["acoustid_api_key"], lambda cfg: cc.check("acoustid", cfg))
_live("soulseek", ["soulseek_username", "soulseek_password"],
      lambda cfg: cc.check("soulseek", cfg))
_live("login", [], lambda cfg: cc.check("login", cfg))

cap.close()

print(f"\n{len(FAILED)} failure(s)")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("All credentials verified on the wire.")
sys.exit(0)
