"""ONE process-wide ``httpx.Client`` for the app's provider calls.

Every provider call in :mod:`server.integrations`, :mod:`server.discovery`,
:mod:`server.streaming_playlists` and :mod:`server.artcache` used to build its
own client — either explicitly, or implicitly through the module-level
``httpx.get``/``httpx.post`` helpers, which construct a client and close it
again per call. Constructing one is not free: it builds an ``ssl.SSLContext``
(measured 55 ms) and loads the CA bundle, so a fresh ``httpx.Client()``
construct+close measured 333 ms here — and a library-wide pass that makes ~100
provider requests paid that setup for connections it then threw away (one
script-8 run over 12 albums made 108 requests).

What this module owns:

* the ONE client every such call shares, built LAZILY on first use under a
  lock, so importing it costs nothing and a process that never talks to a
  provider never opens a socket;
* a DISCARDING cookie jar. ``httpx.get`` created (and dropped) a client per
  call, so a response's ``Set-Cookie`` never reached a later, unrelated
  request; a shared client would silently accumulate cookies and send them to
  that domain again — including a cookie one user's session obtained when
  another user's request follows. Sessions the app manages itself (RYM's
  pasted jar) are passed per request and are untouched by this;
* bounded keep-alive, so an idle FastAPI process or a test run does not hold
  sockets open, and a close on interpreter exit;
* :func:`install`, which points the module-level ``httpx.get``/``httpx.post``
  at the shared client. Those two are the app's provider SEAM — the suites
  replace ``integrations.httpx.get`` with their own fake — so keeping them the
  call sites' spelling is what lets one shared client serve every site without
  rewriting a single test.

Nothing here decides WHAT is requested: params, headers, cookies, redirects
and timeouts stay per request, exactly as each call site passes them today.
"""
import atexit
import threading

import httpx

# httpx's own module-level default timeout, so a call site that passes none
# behaves exactly as it did through `httpx.get`.
DEFAULT_TIMEOUT_S = 5.0

# A small bounded pool: the app's provider traffic is a handful of concurrent
# requests, and a short keep-alive means an idle process (or the next test in a
# suite) never inherits a live socket.
MAX_CONNECTIONS = 16
MAX_KEEPALIVE_CONNECTIONS = 8
KEEPALIVE_EXPIRY_S = 15.0

_lock = threading.Lock()
_client = None
_installed = False
_originals = {}


def _discard_response_cookies(response):
    """Never remember what a response set.

    ``httpx.get`` built — and dropped — a client per call, so a response's
    ``Set-Cookie`` never reached a later, unrelated request. A shared client
    must not turn that into a session the next request inherits (a cookie one
    user's RYM session obtained would otherwise ride along on another user's
    request), so the jar stores none of its own. Cookies a CALLER passes per
    request are sent exactly as before.
    """
    return None


def _install_discarding_jar(client):
    """Give *client* a jar that stores no response cookies.

    ``httpx.Client.cookies``' setter re-wraps whatever it is given
    (``self._cookies = Cookies(cookies)``), so a subclass handed to the
    constructor — or assigned to the property — loses the override. The policy
    is installed on the instance method the client actually calls
    (``self.cookies.extract_cookies(response)``).
    """
    client.cookies.extract_cookies = _discard_response_cookies
    return client


def client():
    """The shared client, built on first use (thread-safe)."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = _install_discarding_jar(httpx.Client(
                    timeout=DEFAULT_TIMEOUT_S,
                    limits=httpx.Limits(
                        max_connections=MAX_CONNECTIONS,
                        max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                        keepalive_expiry=KEEPALIVE_EXPIRY_S),
                    # httpx's own defaults for everything a call site does not
                    # pass: no implicit redirect following, verified TLS,
                    # trust_env on.
                    follow_redirects=False,
                ))
    return _client


def close():
    """Close the shared client (the next use builds a new one).

    Called on interpreter exit and available to tests, which must not leave a
    pool of sockets behind for the next suite in the process.
    """
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def shared_get(url, **kwargs):
    """``httpx.get``'s signature, reusing the shared client."""
    return client().get(url, **kwargs)


def shared_post(url, **kwargs):
    """``httpx.post``'s signature, reusing the shared client."""
    return client().post(url, **kwargs)


def install():
    """Point the module-level ``httpx.get``/``httpx.post`` at the shared client.

    Idempotent. The originals are kept so :func:`uninstall` can put them back
    (a suite that wants the stock behaviour, or a debugger).
    """
    global _installed
    with _lock:
        if _installed:
            return
        _originals["get"] = httpx.get
        _originals["post"] = httpx.post
        httpx.get = shared_get
        httpx.post = shared_post
        _installed = True
    atexit.register(close)


def uninstall():
    """Restore the stock module-level helpers (tests only)."""
    global _installed
    with _lock:
        if not _installed:
            return
        httpx.get = _originals["get"]
        httpx.post = _originals["post"]
        _installed = False


# Installed on import: whoever imports this module wants the shared client, and
# the import is what makes `httpx.get` the app's one seam.
install()
