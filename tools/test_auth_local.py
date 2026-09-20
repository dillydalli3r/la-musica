#!/usr/bin/env python3
"""Who may use the server without a password, and where the library folder may be pointed.

Two rules that are easy to break silently, and were:

  * the login gate is for CLIENT connections. A request from the machine the
    server runs on — its own browser, a desktop shell, or the host of the
    container it runs in (which arrives through the Docker bridge gateway, never
    from 127.0.0.1) — is never asked for a password; anything else on the
    network is. Before this, a published Docker port made even the host's own
    browser sign in, which is the "why does my local install want a password"
    report.
  * the music folder can be chosen from the UI (GET /api/fs/dirs browses the
    SERVER's filesystem; POST /api/config switches it and carries
    <music>/.mlo along) — EXCEPT when MLO_MUSIC_FOLDER pins it, where the pin is
    what the app uses and the stored value is rewritten on every save. A picker
    that pretended otherwise would look like it worked and revert.

Everything here is offline: no network, no server process, no real library.
"""
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402

from server import auth as auth_mod  # noqa: E402
from server import main as main_mod  # noqa: E402

FAILURES = []


def check(label, cond):
    if not cond:
        FAILURES.append(label)
        print(f"FAIL {label}")


class FakeRequest:
    """The only things auth reads off a request: the client address, the headers
    (X-Forwarded-For is deliberately NOT trusted) and the cookies the browser
    carries the session in."""

    def __init__(self, host, headers=None, cookies=None, query=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}


# --------------------------------------------------------------------------- #
# Local, or a client
# --------------------------------------------------------------------------- #
for ip in ("127.0.0.1", "::1", "127.5.5.5"):
    check(f"{ip} is this machine", auth_mod.is_local_ip(ip))
check("the IPv4-mapped form of loopback is this machine too",
      auth_mod.is_local_ip("::ffff:127.0.0.1"))
for ip in ("192.168.1.50", "10.0.0.7", "203.0.113.9", "", "not-an-ip"):
    check(f"{ip!r} is a client, not this machine", not auth_mod.is_local_ip(ip))

own = auth_mod.local_addresses()
check("this machine's own addresses count as local", all(auth_mod.is_local_ip(ip) for ip in own))
check("...and they include loopback", {"127.0.0.1", "::1"} <= own)

GATE_ON = {"required": True, "mode": "auto"}
check("a client on the network must sign in",
      auth_mod.requires_login(FakeRequest("192.168.1.50"), GATE_ON) is True)
check("this machine never has to",
      auth_mod.requires_login(FakeRequest("127.0.0.1"), GATE_ON) is False)
check("auth_mode=required asks even this machine",
      auth_mod.requires_login(FakeRequest("127.0.0.1"),
                              {"required": True, "mode": "required"}) is True)
check("a loopback bind asks nobody",
      auth_mod.requires_login(FakeRequest("127.0.0.1"),
                              {"required": False, "mode": "auto"}) is False)
check("...and a client is only gated when the server demands it at all",
      auth_mod.requires_login(FakeRequest("192.168.1.50"),
                              {"required": False, "mode": "auto"}) is False)

# A forged header must not buy a password-free session: `client_ip` is the
# socket's peer, never X-Forwarded-For (which any caller can set).
forged = FakeRequest("192.168.1.50", {"x-forwarded-for": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
check("a spoofed X-Forwarded-For does not make a client local",
      auth_mod.client_ip(forged) == "192.168.1.50"
      and auth_mod.requires_login(forged, GATE_ON) is True)

# What /api/auth/status reports, per request. The gate state is pinned so the
# answer does not depend on the checkout's own config (a loopback install has
# the gate off, which would make these assertions vacuous).
from server.api_auth import status as status_endpoint  # noqa: E402

real_state = auth_mod.current_state
auth_mod.current_state = lambda refresh=False: dict(GATE_ON, has_password=True, username="",
                                                   public_url="", session_days=30)
try:
    local_state = status_endpoint(FakeRequest("127.0.0.1"))
    remote_state = status_endpoint(FakeRequest("192.168.1.50"))
finally:
    auth_mod.current_state = real_state

check("a local client is told it may come in",
      local_state["local"] is True and local_state["required"] is False
      and local_state["authenticated"] is True)
check("a client is told the server's own answer",
      remote_state["local"] is False and remote_state["gate"] is True)
check("a client on a gated server is not 'authenticated' for free",
      remote_state["required"] is True and remote_state["authenticated"] is False)

# --------------------------------------------------------------------------- #
# Browsing the SERVER's folders (the picker)
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, "Album"))
    os.makedirs(os.path.join(tmp, ".hidden"))
    with io.open(os.path.join(tmp, "Album", "track.flac"), "w", encoding="utf-8") as fh:
        fh.write("not really audio")
    with io.open(os.path.join(tmp, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("a file, not a folder")

    listing = main_mod.fs_dirs(tmp)
    check("only folders are listed", [d["name"] for d in listing["dirs"]] == [".hidden", "Album"]
          or [d["name"] for d in listing["dirs"]] == ["Album", ".hidden"])
    check("a folder holding audio is flagged", 
          next(d for d in listing["dirs"] if d["name"] == "Album")["library"] is True)
    check("a folder without audio is not",
          next(d for d in listing["dirs"] if d["name"] == ".hidden")["library"] is False)
    check("the path and its parent come back",
          listing["path"] == os.path.abspath(tmp)
          and listing["parent"] == os.path.dirname(os.path.abspath(tmp)))
    check("roots are offered for navigation", listing["roots"] == ["/"] if os.name != "nt" else bool(listing["roots"]))
    check("nothing pins the folder here", listing["pinned"] is None)

    # Errors are answers, not tracebacks.
    for bad, expected in ((os.path.join(tmp, "nope"), 404), (os.path.join(tmp, "notes.txt"), 400)):
        try:
            main_mod.fs_dirs(bad)
            check(f"{os.path.basename(bad)} is refused", False)
        except HTTPException as e:
            check(f"{os.path.basename(bad)} is refused with {expected}", e.status_code == expected)

# --------------------------------------------------------------------------- #
# MLO_MUSIC_FOLDER pins the folder; the API says so instead of reverting
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as pinned, tempfile.TemporaryDirectory() as other:
    real = os.environ.get("MLO_MUSIC_FOLDER")
    # save_config is stubbed out: a test that runs on a checkout must not write
    # a temp folder into the config it shares with the app (which is exactly
    # what an earlier version of this file did).
    real_save = main_mod.save_config
    saved = []
    main_mod.save_config = lambda cfg: (saved.append(dict(cfg)), True)[1]
    os.environ["MLO_MUSIC_FOLDER"] = pinned
    try:
        check("the listing reports the pin", main_mod.fs_dirs(pinned)["pinned"] == pinned)
        try:
            main_mod.set_config({"music_folder": other})
            check("switching away from a pinned folder is refused", False)
        except HTTPException as e:
            check("switching away from a pinned folder is refused",
                  e.status_code == 400 and "MLO_MUSIC_FOLDER" in e.detail)
        try:
            main_mod.set_config({"music_folder": os.path.join(pinned, "does-not-exist")})
            check("a folder that is not there is refused", False)
        except HTTPException as e:
            check("a folder that is not there is refused",
                  e.status_code == 400 and "does not exist" in e.detail)
        check("neither refusal reached a save", saved == [])
        # The folder already in force is not a change: a settings save that
        # echoes it back must go through.
        main_mod.set_config({"music_folder": pinned})
        check("re-saving the pinned folder itself is allowed",
              len(saved) == 1 and saved[0].get("music_folder") == pinned)
    finally:
        main_mod.save_config = real_save
        if real is None:
            os.environ.pop("MLO_MUSIC_FOLDER", None)
        else:
            os.environ["MLO_MUSIC_FOLDER"] = real

if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_auth_local")
sys.exit(0)
