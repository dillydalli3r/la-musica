#!/usr/bin/env python3
"""Verification for `server.soulseek_port` — the "Test port" probe.

What a port check must never do is claim more than it observed, so this pins the
things that decide the answer:

  * the LISTENER row, against REAL sockets this suite opens: a port with a
    listener reads `ok`, a port with nothing on it reads `fail` with the reason,
    and a port held by a socket that accepts nothing is told apart from it by the
    bind test (which is what that test is for);
  * the MAPPING row, against stubbed gateways: every state `mlo.portmap` reports
    lands on the row state `GATEWAY_STATES` documents, a mapping the app was told
    about but cannot read back is a warning (never "open"), and an entry whose
    absence proves nothing is `unknown` rather than a failure;
  * the ADDRESS and SELF-CONNECT rows: a carrier-grade NAT or a double NAT is
    NAMED as that, and a refused connection to the public address is `unknown` —
    never `fail`, because a router without NAT hairpinning refuses exactly that
    while the port may still be open to the outside.

Nothing here touches the network beyond loopback: every gateway seam
(`portmap.read_port`, `default_gateway`, `local_ip`) and slskd's REST call are
stubbed, and the one real gateway read this makes — against a loopback port where
nothing answers — is what proves the probe is BOUNDED instead of hanging.

Run:  python tools/test_soulseek_port.py
"""
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mlo.portmap as portmap  # noqa: E402
import server.soulseek as soulseek  # noqa: E402
import server.soulseek_port as sp  # noqa: E402

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


# The address reserved for documentation: a connection to it never answers, so it
# is how the connect budget is measured without leaving this machine's network.
UNROUTABLE = "192.0.2.1"
WAN = "8.8.8.8"
LAN = "192.168.1.5"
GATEWAY = "192.168.1.1"

# --------------------------------------------------------------------------- #
# The seams: everything that would talk to a router or to slskd
# --------------------------------------------------------------------------- #
real_connect = sp._connect
soulseek.client_running = lambda cfg=None: True   # an slskd of ours is answering
soulseek.server_state = lambda cfg=None: {"isLoggedIn": True}
soulseek.listen_port_error = lambda: ""           # neither log is read in a test
soulseek.login_error = lambda cfg=None: ""
portmap.default_gateway = lambda: GATEWAY
portmap.local_ip = lambda gateway="": LAN
reads = {"value": portmap._out("no_gateway", method="upnp",
                               detail="no device answered the UPnP search")}
portmap.read_port = lambda port, **kw: dict(reads["value"], listen_port=port)


def read(state, **kw):
    """A gateway read as `mlo.portmap.read_port` reports one."""
    return portmap._out(state, ok=state == "mapped", method="upnp", **kw)


def seed(result=None, port=0):
    """One result in the app's stored mapping state, as its reconciler leaves it."""
    with soulseek._PORTMAP_LOCK:
        soulseek._PORTMAP.update({"result": result, "port": port,
                                  "checked_at": time.time() if result else 0.0,
                                  "in_flight": False, "reason": "test"})


def row(payload, cid):
    """One check out of a payload."""
    return next(c for c in payload["checks"] if c["id"] == cid)


def drain(sock):
    """Accept and drop connections, so the accept queue never fills: a full
    backlog stops completing connects, which would read as "nothing listening"."""
    while True:
        try:
            conn, _peer = sock.accept()
        except OSError:
            return
        conn.close()


# --------------------------------------------------------------------------- #
# 1) the listener, against real sockets
# --------------------------------------------------------------------------- #
print("== the listener row, against real sockets ==")
# A listener on the WILDCARD address, which is what slskd binds — so a bind test on
# that port has to fail, and that is exactly how the app tells "slskd is listening"
# apart from "the port is free".
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("", 0))
listener.listen(64)
live_port = listener.getsockname()[1]
threading.Thread(target=drain, args=(listener,), daemon=True).start()
# A port nothing holds: bound to learn a free one, then released.
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.bind(("", 0))
free_port = probe.getsockname()[1]
probe.close()
# A port held by a socket that accepts nothing: bound, never listening.
held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
held.bind(("", 0))
held_port = held.getsockname()[1]

started = time.time()
live = sp.port_check({"soulseek_listen_port": live_port, "soulseek_upnp": True})
empty = sp.port_check({"soulseek_listen_port": free_port, "soulseek_upnp": True})
busy = sp.port_check({"soulseek_listen_port": held_port, "soulseek_upnp": True})
elapsed = time.time() - started

ok(row(live, "listen")["state"] == "ok",
   f"a port with a listener reads ok ({row(live, 'listen')['detail']})")
ok(str(live_port) in row(live, "listen")["detail"] and "accepted" in row(live, "listen")["detail"],
   "…and the row says a connection to that port was accepted")
ok(empty["port"] == free_port and row(empty, "listen")["state"] == "fail",
   "a port with nothing on it reads fail")
ok("nothing is listening" in row(empty, "listen")["detail"],
   f"…with the reason ({row(empty, 'listen')['detail']})")
ok(row(busy, "listen")["state"] == "fail" and "cannot be bound" in row(busy, "listen")["detail"],
   "a port held but not accepting is told apart from a free one by the bind test")
ok(elapsed < 15.0,
   f"three probes against real sockets finished in {elapsed:.2f}s — nothing waits "
   f"on a connection that will not answer")

# The real connect, in all three of its outcomes.
started = time.time()
accepted, why = sp._connect("127.0.0.1", live_port, 0.5)
ok(accepted is True and time.time() - started < 3.0,
   "the real connect accepts a listener that is there")
started = time.time()
refused, why = sp._connect("127.0.0.1", held_port, 0.5)
ok(refused is False and time.time() - started < 3.0,
   f"…does not accept on a bound-but-not-listening port ({why}), within the budget")
started = time.time()
nowhere, why = sp._connect(UNROUTABLE, 50000, 0.5)
ok(nowhere is False and time.time() - started < 3.0,
   f"…and gives up on an address that answers nothing ({why})")

# From here on the connection to the public address is stubbed: the real call's
# outcomes and its budget are proven above, and the probe below is never allowed
# to dial out of this machine.
sp._connect = lambda host, port, timeout: (False, "connection refused")

# The real gateway reader, with nothing answering on loopback: bounded, and honest
# that it could not read anything.
blackhole = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
blackhole.bind(("127.0.0.1", 0))
quiet_port = blackhole.getsockname()[1]
blackhole.close()
started = time.time()
blank = portmap.read_port(50000, gateway="127.0.0.1", timeout=0.3,
                          ssdp_addr="127.0.0.1", ssdp_port=quiet_port,
                          pmp_port=quiet_port)
elapsed = time.time() - started
ok(blank["state"] in ("no_gateway", "unsupported") and blank["ok"] is False,
   f"a gateway that answers nothing is never reported as a mapping ({blank['state']})")
ok(elapsed < 10.0, f"…and that real read came back in {elapsed:.2f}s")

# --------------------------------------------------------------------------- #
# 2) the mapping row: every gateway state onto its row state
# --------------------------------------------------------------------------- #
print("== the mapping row, per gateway state ==")
seed(None)
cfg = {"soulseek_listen_port": live_port, "soulseek_upnp": True}

ok(sp.GATEWAY_STATES["mapped"] == "ok" and sp.GATEWAY_STATES["refused"] == "fail",
   "a listing the router confirms is ok; the router's own refusal is fail")
ok({sp.GATEWAY_STATES[s] for s in ("unsupported", "no_gateway")} == {"warn"},
   "a gateway that cannot be read is a warning, because a forward may still exist")
ok({sp.GATEWAY_STATES[s] for s in ("off", "pending", "checking", "client_down")}
   == {"unknown"}, "the app's own 'nothing was asked' states are unknown, never a pass")

reads["value"] = read("mapped", port=live_port, ip=LAN, external=WAN, verified=True,
                      gateway=GATEWAY,
                      detail="the gateway lists external port %d -> %s:%d"
                             % (live_port, LAN, live_port))
mapped = sp.port_check(cfg)
ok(row(mapped, "mapping")["state"] == "ok",
   "a mapping the router lists for this machine reads ok")
ok(row(mapped, "address")["state"] == "ok",
   "…and the address row accepts a public WAN address with the mapping pointing here")

reads["value"] = read("refused", port=live_port, ip=LAN, external=WAN,
                      detail="the gateway lists no mapping of port %d "
                             "(NoSuchEntryInArray (UPnP error 714))" % live_port)
refused = sp.port_check(cfg)
ok(row(refused, "mapping")["state"] == "fail"
   and "NoSuchEntryInArray" in row(refused, "mapping")["detail"],
   "a router that lists no mapping is a fail, carrying the router's own words")
ok(refused["verdict"] == "fail" and refused["ok"] is False,
   "…and the verdict is fail, with ok false")

reads["value"] = read("unsupported", port=live_port, ip=LAN, external=WAN,
                      detail="the gateway cannot read a mapping back (Invalid Action)")
unreadable = sp.port_check(cfg)
ok(row(unreadable, "mapping")["state"] == "warn",
   "a gateway that cannot be read back is a warning, not a pass")

reads["value"] = read("no_gateway", port=live_port,
                      detail="no device answered the UPnP search")
nobody = sp.port_check(cfg)
ok(row(nobody, "mapping")["state"] == "warn" and row(nobody, "address")["state"] == "unknown",
   "no gateway answering is a warning for the mapping and unknown for the addresses")

reads["value"] = read("error", port=live_port, ip=LAN,
                      detail="this machine's address could not be determined")
broken = sp.port_check(cfg)
ok(row(broken, "mapping")["state"] == "fail",
   "the local side failing is a fail (there is nothing to forward to)")

# NAT-PMP: a mapping the app was TOLD about and cannot read back is the only word
# that will ever exist for it, so it is a warning that quotes that word.
reads["value"] = read("unsupported", port=live_port,
                      detail="NAT-PMP cannot be asked what it holds")
seed(portmap._out("mapped", ok=True, method="natpmp", port=live_port, ip=LAN,
                  external=WAN, verified=True,
                  detail="NAT-PMP: external port %d is forwarded here for 7200s"
                         % live_port), port=live_port)
stored_mapped = sp.port_check(cfg)
ok(row(stored_mapped, "mapping")["state"] == "warn"
   and "NAT-PMP: external port" in row(stored_mapped, "mapping")["detail"],
   "a mapping this app was told about but cannot read back is a warning quoting it")

# …and one whose granted lease ran out says so: the gateway drops the entry then.
seed(portmap._out("mapped", ok=True, method="natpmp", port=live_port, ip=LAN,
                  verified=True, expires_at=time.time() - 60,
                  detail="NAT-PMP: external port %d is forwarded here for 60s"
                         % live_port), port=live_port)
expired = sp.port_check(cfg)
ok("lease the gateway granted has run out" in row(expired, "mapping")["detail"],
   "a stored mapping whose lease ran out reports that instead of staying silent")
seed(None)

# Automatic port opening OFF: the app asks the router for nothing, so an entry it
# cannot read proves nothing — never a fail, whatever the gateway answered.
reads["value"] = read("refused", port=live_port,
                      detail="the gateway lists no mapping of port %d" % live_port)
off_cfg = {"soulseek_listen_port": live_port, "soulseek_upnp": False}
off = sp.port_check(off_cfg)
ok(row(off, "mapping")["state"] == "unknown"
   and "automatic port opening is off" in row(off, "mapping")["detail"],
   "with automatic opening off, a missing entry is unknown — a hand-made forward "
   "may exist")
ok(off["verdict"] == "unknown" and off["ok"] is False,
   "…and that leaves the verdict unknown rather than a pass")
reads["value"] = read("mapped", port=live_port, ip=LAN, external=WAN, verified=True,
                      detail="the gateway lists external port %d -> %s:%d"
                             % (live_port, LAN, live_port))
off_but_listed = sp.port_check(off_cfg)
ok(row(off_but_listed, "mapping")["state"] == "ok",
   "…while an entry the router does list is still ok, whoever asked for it")
seed(None)

# --------------------------------------------------------------------------- #
# 3) the address row: the shapes no mapping can fix
# --------------------------------------------------------------------------- #
print("== the address row ==")
reads["value"] = read("mapped", port=live_port, ip=LAN, external="100.72.3.4",
                      verified=True, detail="the gateway lists external port %d" % live_port)
cgnat = sp.port_check(cfg)
ok(row(cgnat, "address")["state"] == "fail"
   and "carrier-grade NAT" in row(cgnat, "address")["detail"],
   "a carrier-grade NAT WAN address is named as that (no mapping can ever work)")
ok(cgnat["verdict"] == "fail",
   "…and it fails the verdict instead of being blamed on a firewall")

reads["value"] = read("mapped", port=live_port, ip=LAN, external="10.0.0.2",
                      verified=True, detail="the gateway lists external port %d" % live_port)
double = sp.port_check(cfg)
ok(row(double, "address")["state"] == "fail"
   and "double NAT" in row(double, "address")["detail"],
   "a private WAN address is named as a router behind another NAT")

reads["value"] = read("mapped", port=live_port, ip="192.168.1.99", external=WAN,
                      verified=True, detail="the gateway lists external port %d" % live_port)
other_host = sp.port_check(cfg)
ok(row(other_host, "address")["state"] == "fail"
   and "another device" in row(other_host, "address")["detail"],
   "a mapping pointing at another host is a fail: peers would reach that device")

reads["value"] = read("mapped", port=live_port, ip=LAN, verified=True,
                      detail="the gateway lists external port %d" % live_port)
no_wan = sp.port_check(cfg)
ok(row(no_wan, "address")["state"] == "unknown"
   and "no gateway stated its WAN address" in row(no_wan, "address")["detail"],
   "no stated WAN address leaves the address row unknown, not failed")

# --------------------------------------------------------------------------- #
# 4) the self-connect row: a refusal is never a failure
# --------------------------------------------------------------------------- #
print("== the self-connect row ==")
sp._connect = lambda host, port, timeout: (False, "connection refused")
reads["value"] = read("mapped", port=live_port, ip=LAN, external=WAN, verified=True,
                      detail="the gateway lists external port %d" % live_port)
hairpin = sp.port_check(cfg)
ok(row(hairpin, "self-connect")["state"] == "unknown"
   and "NAT hairpinning" in row(hairpin, "self-connect")["detail"],
   "a connection to the public address that is refused is unknown (no hairpinning), "
   "never fail")
ok(hairpin["verdict"] == "ok" and hairpin["ok"] is True,
   "…and that advisory row cannot lower a verdict the other rows established")

sp._connect = lambda host, port, timeout: (True, "")
answers = sp.port_check(cfg)
ok(row(answers, "self-connect")["state"] == "ok"
   and "was accepted from this machine" in row(answers, "self-connect")["detail"],
   "a WAN address that does answer is reported as accepted (the real connect's "
   "outcomes were proven above)")
# Back to the offline stub for the rest: nothing below may dial out.
sp._connect = lambda host, port, timeout: (False, "connection refused")

# --------------------------------------------------------------------------- #
# 5) slskd's own answer, and the verdict rules
# --------------------------------------------------------------------------- #
print("== slskd's own answer ==")
soulseek.server_state = lambda cfg=None: {"isLoggedIn": False}
soulseek.login_error = lambda cfg=None: "INVALIDPASS"
logged_out = sp.port_check(cfg)
ok(row(logged_out, "network")["state"] == "warn"
   and "INVALIDPASS" in row(logged_out, "network")["detail"],
   "a logged-out daemon is a warning carrying its own words")
soulseek.server_state = lambda cfg=None: None
quiet = sp.port_check(cfg)
ok(row(quiet, "network")["state"] == "warn",
   "a daemon that reports no server state is a warning (logged out looks like that)")
soulseek.server_state = lambda cfg=None: {"isLoggedIn": True}
ok(row(good := sp.port_check(cfg), "network")["state"] == "ok",
   "a daemon that says it is signed in reads ok")
soulseek.client_running = lambda cfg=None: False
down = sp.port_check({"soulseek_listen_port": free_port, "soulseek_upnp": True})
ok(row(down, "network")["state"] == "unknown"
   and "not answering on its web port" in row(down, "network")["detail"],
   "a daemon that is not up cannot say whether it is on the Soulseek network")
ok(row(down, "listen")["state"] == "fail",
   "…and the listener row still reports what this machine does with the port")
soulseek.client_running = lambda cfg=None: True

print("== the verdict and the payload ==")
reads["value"] = read("mapped", port=live_port, ip=LAN, external=WAN, verified=True,
                      gateway=GATEWAY,
                      detail="the gateway lists external port %d -> %s:%d"
                             % (live_port, LAN, live_port))
good = sp.port_check(cfg)
ok(good["verdict"] == "ok" and good["ok"] is True,
   "a listener and a router mapping together read ok")
ok([c["id"] for c in good["checks"]] == ["listen", "publish", "mapping", "address",
                                         "self-connect", "network"],
   "every row is reported, in a stable order")
ok(all({"id", "label", "state", "detail", "proves", "cannot"} <= set(c)
       for c in good["checks"]),
   "every row states what it proves and what it cannot")
ok(all(c["proves"] and c["cannot"] for c in good["checks"]),
   "…and neither of those is ever empty")
ok(good["port"] == live_port, "the payload names the port it is about")
ok("OUTSIDE this network" in good["note"] and "does not ship" in good["note"],
   "the note says the outside half needs a probe this app does not ship")
ok("cannot see which process" in row(good, "listen")["cannot"],
   "the listener row says it cannot see whose socket it is")
ok("OUTGOING connection" in row(good, "network")["cannot"],
   "the login row says a login does not prove the listen port is reachable")

reads["value"] = read("refused", port=live_port, external=WAN, verified=False,
                      detail="the gateway lists no mapping of port %d" % live_port)
broken_chain = sp.port_check(cfg)
ok(broken_chain["verdict"] == "fail" and broken_chain["ok"] is False,
   "a failure anywhere is the verdict")

# --------------------------------------------------------------------------- #
# 6) the host's publish line: measured from inside a container, not assumed
# --------------------------------------------------------------------------- #
print("== the publish row (the host's own port list, read from in here) ==")
# The app's own served port, which compose publishes beside the listen port: the
# control that says whether THIS Docker hands published ports back at all.
free2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
free2.bind(("", 0))
web_port = free2.getsockname()[1]
free2.close()
container_cfg = {"soulseek_listen_port": live_port, "soulseek_upnp": True,
                 "server_port": web_port}
real_container = sp._container
sp._container = lambda: True          # read as a container WITHOUT one


def gateway(accepts):
    """A gateway that accepts exactly the ports in *accepts* (the other ports of
    this machine are never dialled: the seam is the same one the suite already
    replaced to keep the probe off the network)."""
    def connect(host, port, timeout):
        return (port in accepts, "" if port in accepts else "connection refused")
    return connect


reads["value"] = read("no_gateway", port=live_port,
                      detail="no device answered the UPnP search")
sp._connect = gateway({live_port})
published = sp.port_check(container_cfg)
ok(row(published, "publish")["state"] == "ok",
   f"a port the host hands back reads ok ({row(published, 'publish')['detail']})")
ok(str(live_port) in row(published, "publish")["label"],
   "…and the row names the port it is about")
ok(row(published, "publish")["detail"].find("no UDP port") > 0
   and "obfuscated" in row(published, "publish")["detail"],
   "…and states which ports have to be reachable from the internet: TCP only, "
   "no second obfuscated port")

# The R279 class the numbers alone cannot rule out: the host publishes SOMETHING
# (its own web port answers on the gateway) but not the port slskd listens on —
# so the compose file is wrong and the router is not the first thing to fix.
sp._connect = gateway({web_port})
unpublished = sp.port_check(container_cfg)
pub_row = row(unpublished, "publish")
ok(pub_row["state"] == "fail",
   f"a port the host does not publish is a fail, not a warning ({pub_row['detail']})")
ok(f"ports: \"{live_port}:{live_port}\"" in pub_row["detail"]
   and "MLO_SOULSEEK_LISTEN_PORT" in pub_row["detail"],
   "…and the fail carries the exact compose line and the pin that seeds it")
ok(unpublished["verdict"] == "fail" and unpublished["ok"] is False,
   "…and it is the verdict: peers cannot reach a port nobody publishes")

# A Docker that does not hand published ports back into the container (the
# control refused too) cannot be read from in here — so the row says that instead
# of accusing the compose file of a mistake it cannot see.
sp._connect = gateway(set())
unreadable = sp.port_check(container_cfg)
ok(row(unreadable, "publish")["state"] == "warn"
   and "docker port <container>" in row(unreadable, "publish")["detail"],
   "a host that reflects no published port leaves the publish row unread, never fail")

# Nothing accepts on the port inside the container: a refusal on the gateway
# would say nothing about the publish line, and the listener row is the one that
# already failed.
sp._connect = gateway({live_port})
sp._container = lambda: False
not_a_container = sp.port_check({"soulseek_listen_port": live_port,
                                 "soulseek_upnp": True})
ok(row(not_a_container, "publish")["state"] == "unknown"
   and "not a container" in row(not_a_container, "publish")["detail"],
   "a direct install has no publish line to read, and says so instead of passing")
sp._container = lambda: True
sp._connect = gateway({live_port})
nothing_inside = sp.port_check({"soulseek_listen_port": free_port,
                                "soulseek_upnp": True, "server_port": web_port})
ok(nothing_inside["port"] == free_port
   and row(nothing_inside, "publish")["state"] == "unknown"
   and "nothing accepts on TCP" in row(nothing_inside, "publish")["detail"],
   "with nothing listening inside, the publish row stays unknown (a published "
   "port reads refused there too)")
sp._container = real_container
sp._connect = lambda host, port, timeout: (False, "connection refused")

listener.close()
held.close()
seed(None)

print(f"\nAll {passed} checks passed.")
