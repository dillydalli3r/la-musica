#!/usr/bin/env python3
"""The app's provider HTTP goes through ONE shared client.

Every provider call used to build its own ``httpx.Client`` — explicitly, or
through the module-level ``httpx.get``/``httpx.post`` helpers, which construct
and close a client per call. A client's construction loads a TLS context
(measured 55 ms) and a fresh ``httpx.Client()`` construct+close measured
333 ms, so a library-wide pass making ~100 provider requests paid tens of
seconds for connections it threw away (one script-8 run over 12 albums made
108 requests).

The suites stub providers by replacing ``integrations.httpx.get`` (that
module attribute IS the seam), so a call site that goes back to building its
own container is invisible to them and expensive in production. This suite
asserts the CONTRACT by counting constructor calls — no timings — with the
shared client warmed and a stubbed transport mounted:

  * one client, process-wide;
  * every app seam — the module-level get/post the providers use, the
    streaming cover/probe seams, discovery's JSON seam and artcache's image
    seam — reaches that client and builds NO new one (call count 0);
  * and the shared client keeps no cookie jar, because the module-level API it
    replaces dropped response cookies with the client that received them.

Run:  python tools/test_http_client.py     (exit 1 on failure, 2 when httpx
is missing)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed = 0
skipped = []


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def skip(label):
    skipped.append(label)
    print(f"  skip: {label}")


def main():
    try:
        import httpx
    except ImportError:
        print("httpx missing — nothing to check")
        return 2

    from server import artcache, discovery, httpclient, integrations
    from server import streaming_playlists

    # ---- one client, process-wide ---------------------------------------
    first = httpclient.client()
    ok(httpclient.client() is first,
       "httpx: the app has ONE client (two calls, same object)")
    ok(getattr(httpx.get, "__module__", "") == "server.httpclient"
       and getattr(httpx.post, "__module__", "") == "server.httpclient",
       "httpx: the module-level get/post helpers ARE the shared-client seam "
       "(the provider suites' `integrations.httpx.get` hook still works)")

    # ---- no seam builds its own client ----------------------------------
    made = []
    real_init = httpx.Client.__init__

    def counting_init(self, *a, **kw):
        made.append(1)
        return real_init(self, *a, **kw)

    shared = httpclient.client()
    real_transport = shared._transport
    served = []
    PAYLOAD = {"ok": True, "title": "x"}

    def handler(request):
        served.append(str(request.url))
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0" + b"j" * 64,
                                  headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json=PAYLOAD)

    httpx.Client.__init__ = counting_init
    shared._transport = httpx.MockTransport(handler)
    try:
        mb = integrations.mb_get("release/1", retries=1)          # httpx.get
        post, err = integrations._advisory_post(                  # httpx.post
            "https://api.example/token", data={"grant_type": "client_credentials"})
        probe = integrations._probe_get("https://img.example/front.jpg",
                                        nbytes=4)
        found = discovery._json("https://api.example/discovery")   # httpx.get
        pl, pl_err, _meta = streaming_playlists._get(              # httpx.get
            "https://api.example/playlist")
        status, image, ctype = artcache._get(                      # client.stream
            "https://img.example/cover.jpg", {"User-Agent": "t"}, 5.0)
    finally:
        shared._transport = real_transport
        httpx.Client.__init__ = real_init

    ok(not made,
       f"no seam builds its own client — the shared one serves them all (was "
       f"one construction per request, measured {len(made)} constructions for "
       f"{len(served)} request(s))")
    ok(httpclient.client() is first,
       "httpx: the shared client is still the same object after those calls")
    ok(len(served) >= 6,
       f"the seams really used it ({len(served)} request(s) reached the "
       f"stubbed transport: {', '.join(sorted(set(served)))})")
    ok(mb == PAYLOAD and post == PAYLOAD and err == "",
       f"MB and the advisory POST still return the body ({mb}, {post!r}, "
       f"{err!r})")
    ok(found == PAYLOAD and pl == PAYLOAD and pl_err == "",
       f"discovery and the playlist seam still return the body ({found}, {pl})")
    ok(probe == b"\xff\xd8\xff\xe0",
       f"the cover probe still answers with the response's first bytes "
       f"({probe!r})")
    ok(status == 200 and image and ctype == "image/jpeg",
       f"artcache still streams an image ({status}, {len(image or b'')} bytes, "
       f"{ctype!r})")

    # ---- the shared client keeps no cookie jar --------------------------
    # The per-call client `httpx.get` built dropped response cookies with
    # itself, so a shared client must not accumulate a session (a cookie one
    # user's RYM session obtained would ride along on another user's request).
    seen = []

    def cookie_handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True},
                              headers={"set-cookie":
                                       "session=1; Path=/; Domain=api.example"})

    shared._transport = httpx.MockTransport(cookie_handler)
    try:
        shared.get("https://api.example/session")
        shared.get("https://api.example/next", cookies={"mine": "1"})
    finally:
        shared._transport = real_transport
    ok(not list(shared.cookies.jar),
       f"httpx: the shared client stores no response cookie (measured "
       f"{list(shared.cookies.jar)})")
    ok(seen[0].get("cookie") is None,
       f"httpx: and an unrelated request carries none of the session's "
       f"cookies ({seen[0].get('cookie')!r})")
    ok(seen[1].get("cookie") == "mine=1",
       f"httpx: a caller's OWN per-request cookie is still sent "
       f"({seen[1].get('cookie')!r}) — RYM's pasted jar is untouched")

    if skipped:
        print(f"\n{passed} check(s) passed, {len(skipped)} skipped")
    else:
        print(f"\n{passed} check(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
