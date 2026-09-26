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
    NAMED as that, a 100.64.0.0/10 address on this machine's OWN route to the
    internet is told apart from one the carrier hands out by the routes (a tunnel
    carrying the host's traffic, fixed here, vs the line's CGNAT, which is the
    ISP's — opposite remedies for the same numbers), and a refused connection to
    the public address is `unknown` — never `fail`, because a router without NAT
    hairpinning refuses exactly that while the port may still be open to the
    outside;

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

# The two shapes one 100.64.0.0/10 address can be, told apart by the ROUTES and
# never by the address — measured on the owner's install, where the host's traffic
# left through a Tailscale exit node: egress 87.249.138.224 while the router's own
# WAN was a different, public address, so every row about the router was green and
# a peer's browse still could not land. A carrier's CGNAT and a tunnel wear the
# same numbers with opposite remedies, so each sentence has to name its own.

def local_ips(lan, egress):
    """`local_ip` as a machine really answers it: the route to the gateway, and
    the route to the internet with no hint (the OS's own choice)."""
    return lambda gateway="": (lan if gateway else egress)


portmap.local_ip = local_ips("192.168.40.62", "100.72.6.55")
tunnel_cfg = {"soulseek_listen_port": live_port, "soulseek_upnp": True,
              "soulseek_router_ip": "192.168.40.1"}
reads["value"] = read("mapped", port=live_port, ip="192.168.40.62",
                      external="216.212.53.255", verified=True,
                      gateway="192.168.40.1",
                      detail="the gateway lists external port %d" % live_port)
tunnel = sp.port_check(tunnel_cfg)
addr = row(tunnel, "address")
ok(addr["state"] == "fail" and "TUNNEL" in addr["detail"],
   "a 100.64.0.0/10 address on this machine's own route to the internet is a "
   "tunnel, not a carrier's CGNAT")
ok("100.72.6.55" in addr["detail"] and "192.168.40.62" in addr["detail"],
   "…and it states what was measured: the address the internet is reached by and "
   "the one the router reaches this machine on")
ok("exit node" in addr["detail"] and "split-route" in addr["detail"],
   "…with the remedy for THAT shape (leave the exit node / split-route the host)")
ok("Ask the ISP" not in addr["detail"],
   "…and never the carrier's remedy for it: no call to the ISP can change a tunnel")
ok("the gateway states 216.212.53.255" in addr["detail"],
   "…while the router's own, public WAN is still reported above it, which is "
   "exactly the green-everywhere shape the owner hit")
ok(row(tunnel, "mapping")["state"] == "ok" and tunnel["verdict"] == "fail"
   and tunnel["ok"] is False,
   "…and the verdict fails on it: every other row passed and peers still dial an "
   "address no forward can serve")
ok("tunnel's provider happens to forward" in addr["cannot"],
   "…and the row still says what it cannot know: whether the tunnel's own "
   "provider forwards the port despite this")
ok("100.64.0.0/10 egress" in addr["cannot"],
   "…including its own limit: a VPN whose interface holds any other address is "
   "not named as this shape")

# A route table whose own default hop is in the range: that is a tunnel's own
# route (an exit node's next hop), and the machine's address on the router's route
# is then the same one — the hop is the difference, and the sentence says so.
portmap.local_ip = local_ips("100.72.6.55", "100.72.6.55")
hop_cfg = {"soulseek_listen_port": live_port, "soulseek_upnp": True,
           "soulseek_router_ip": "100.64.0.1"}
reads["value"] = read("mapped", port=live_port, ip="100.72.6.55",
                      external=WAN, verified=True, gateway="100.64.0.1",
                      detail="the gateway lists external port %d" % live_port)
hop = sp.port_check(hop_cfg)
ok(row(hop, "address")["state"] == "fail"
   and "next hop of its own (100.64.0.1" in row(hop, "address")["detail"],
   "a default hop inside 100.64.0.0/10 is read as the tunnel's own route")

# …and the OTHER shape the same block can be: the line hands this machine itself a
# CGNAT address (a bridged modem), where the address on the router's route IS the
# address the internet is reached by — no local setting can fix that one, so the
# remedy is the ISP's and the tunnel's advice must not appear.
portmap.local_ip = local_ips("100.64.5.20", "100.64.5.20")
line_cfg = {"soulseek_listen_port": live_port, "soulseek_upnp": True,
            "soulseek_router_ip": "100.64.5.1"}
reads["value"] = read("mapped", port=live_port, ip="100.64.5.20",
                      external="100.64.9.9", verified=True, gateway="100.64.5.1",
                      detail="the gateway lists external port %d" % live_port)
line = sp.port_check(line_cfg)
addr = row(line, "address")
ok(addr["state"] == "fail" and "carrier-grade NAT" in addr["detail"]
   and "Ask the ISP" in addr["detail"],
   "the same address as THIS machine's own on the router's network is the line's "
   "CGNAT, and the remedy is the ISP's")
ok("TUNNEL" not in addr["detail"] and "exit node" not in addr["detail"],
   "…and the tunnel's remedy is not offered for it")

# …and when the route to the router cannot be read, which shape it is cannot be
# told apart at all: the row says that instead of picking a remedy (an exit node's
# advice for a carrier line, or the ISP for a tunnel, are both wrong half the time).
portmap.local_ip = local_ips("", "100.72.6.55")
reads["value"] = read("mapped", port=live_port, ip=LAN, external=WAN, verified=True,
                      detail="the gateway lists external port %d" % live_port)
unread_router = sp.port_check({"soulseek_listen_port": live_port,
                               "soulseek_upnp": True})
addr = row(unread_router, "address")
ok(addr["state"] == "warn" and "cannot be told apart" in addr["detail"],
   "an unreadable route to the router leaves the shape unclaimed, as a warning")
ok("Ask the ISP" not in addr["detail"] and "exit node off" not in addr["detail"],
   "…naming neither remedy as the answer")
portmap.local_ip = lambda gateway="": LAN

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
# The router a user NAMES for the mapping is a different machine from the host
# this container runs on, and the publish line must keep asking the container's
# own gateway. Seen live: with soulseek_router_ip set, this probe was sent to the
# router, which answered nothing about a port list it does not keep — turning a
# measured `ok` into a `warn` about the host's publish line.
dialled = []


def dials(accepts):
    def connect(host, port, timeout):
        dialled.append(host)
        return (port in accepts, "" if port in accepts else "connection refused")
    return connect


sp._connect = dials({live_port})
reads["value"] = read("no_gateway", port=live_port,
                      detail="no device answered the UPnP search")
named = sp.port_check({"soulseek_listen_port": live_port, "soulseek_upnp": True,
                       "server_port": web_port, "soulseek_router_ip": "192.168.40.1"})
ok(row(named, "publish")["state"] == "ok",
   f"a named router does not move the publish probe off the container's gateway "
   f"({row(named, 'publish')['detail']})")
ok(set(dialled) == {GATEWAY},
   f"…the address dialled is this container's own gateway ({sorted(set(dialled))})")

# Inside a container the mapping MUST point at the HOST's address on the router's
# network, never at this process's own: the host publishes the port straight back
# into this container, so reading that difference as "peers would reach another
# device" is the opposite of the truth once that address answers on the port.
reads["value"] = read("mapped", port=live_port, ip="192.168.40.62",
                      external=WAN, gateway="192.168.40.1", verified=True,
                      detail=f"the gateway lists external port {live_port} -> "
                             f"192.168.40.62:{live_port}, and 192.168.40.62 "
                             f"accepts a connection there")
bridged_cfg = {"soulseek_listen_port": live_port, "soulseek_upnp": True,
               "server_port": web_port, "soulseek_router_ip": "192.168.40.1"}
real_local_ip = portmap.local_ip
portmap.local_ip = lambda gateway="": "172.18.0.3"   # the container's own view
sp._connect = dials({live_port})
bridged = sp.port_check(bridged_cfg)
addr = row(bridged, "address")
ok(addr["state"] == "ok",
   f"a mapping that points at the HOST reads ok inside a container ({addr['detail']})")
ok("HOST's address" in addr["detail"] and "another device" not in addr["detail"],
   "…and it says whose address that is instead of accusing the mapping")
ok(bridged["verdict"] != "fail",
   f"…and the verdict is not the fail it used to be ({bridged['verdict']})")

# …and the ONE thing in front of the host that a container cannot see at all: the
# host's own route out. An address in the carrier range read from IN HERE is not
# evidence of anything (this process answers on Docker's bridge), so the row must
# not name a tunnel from it — the note on the mapping row asks the owner to make
# that comparison on the host instead, which is the only place it can be made.
portmap.local_ip = lambda gateway="": "100.72.6.55"
sp._connect = dials({live_port})
from_bridge = sp.port_check(bridged_cfg)
ok("TUNNEL" not in row(from_bridge, "address")["detail"]
   and row(from_bridge, "address")["state"] == "ok",
   "a range address measured inside a container is never called a tunnel: the "
   "host's routing table is not visible from in there")
ok("host's traffic leaves through a VPN or a Tailscale exit node"
   in row(from_bridge, "mapping")["detail"],
   "…and the note on the mapping row names the trap instead")
ok("api.ipify.org" in row(from_bridge, "mapping")["detail"]
   and "the WAN address the router's admin page shows"
   in row(from_bridge, "mapping")["detail"],
   "…with the comparison to make on the HOST and what to compare it against")
portmap.local_ip = lambda gateway="": "172.18.0.3"

# The same numbers on a DIRECT install are still the real failure they are.
portmap.local_ip = real_local_ip
sp._container = lambda: False
direct = sp.port_check(bridged_cfg)
ok(row(direct, "address")["state"] == "fail",
   "the same mismatch on a direct install is still a fail")
sp._container = lambda: True
sp._connect = dials({live_port})

sp._container = real_container
sp._connect = lambda host, port, timeout: (False, "connection refused")

listener.close()
held.close()
seed(None)

print(f"\nAll {passed} checks passed.")
