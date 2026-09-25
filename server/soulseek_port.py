"""Can peers reach this app's Soulseek listen port — the probe behind "Test port".

The honest scope first, because it decides every state below: a definite "open to
the internet" answer needs a probe from OUTSIDE this network, and this app ships
none (nothing here contacts a third-party service). What IS available is a chain
of things that can be observed from this machine, and every row says which link
of it that row is:

  * listen        a real TCP connection to 127.0.0.1:<port> plus — when nothing
                  answers — the bind test that separates "slskd is not listening"
                  from "another process holds the port";
  * publish       in a container, the host's OWN port list as the container can
                  see it: a published port is accepted on Docker's gateway from
                  in here and an unpublished one is not, so "the compose file
                  publishes the number slskd listens on" is measured instead of
                  assumed (and the app's own served port is the control, so a
                  refusal is only called a missing publish line when this Docker
                  really does hand published ports back);
  * mapping       what the gateway holds for that port right now
                  (`mlo.portmap.read_port`), with this app's own stored verdict as
                  the fallback a NAT-PMP gateway leaves (it has no request that
                  reads an entry back, so the answer that MADE the mapping is the
                  only word that will ever exist for it);
  * address       the LAN address the mapping points at vs this machine, and the
                  WAN address the gateway states — so a CGNAT or double NAT setup,
                  where NO port mapping can ever work, is named as that instead of
                  being blamed on a firewall;
  * self-connect  a TCP connection from here to <WAN address>:<port>. A router
                  without NAT hairpinning refuses that while the port is still
                  open to the outside, so this row can never fail;
  * network       slskd's own answer about its Soulseek login — the strongest
                  signal available without an outside observer, though a login
                  travels over the outgoing connection.

Nothing here is guessed and nothing here changes anything: no mapping is added or
removed, and no lock is taken, so a download in flight is no reason to refuse a
look at the port.
"""
import ipaddress
import os
import socket
import time
from datetime import datetime, timezone

from mlo.config import load_config

# The probe is a button in the UI, so each network step gets its own small
# budget: a router that never answers must not turn the button into a spinner
# that never ends.
LOCAL_TIMEOUT = 0.4    # loopback answers at once; this only bounds a black hole
PUBLIC_TIMEOUT = 2.0   # an unanswered WAN connect is a router that will not hairpin
GATEWAY_TIMEOUT = 1.5  # one UPnP discovery, per search target
DEFAULT_PORT = 50000   # the app's own default listen port (soulseek_listen_port)

# The port this app is served on when nothing says otherwise — the compose file's
# OTHER published port, and therefore the control the publish row is read against.
DEFAULT_WEB_PORT = 8000

# What a peer in front of this host has to be able to reach, as the readout puts
# it: the listen port and NOTHING ELSE. slskd's inbound side is one TCP listener
# ("Listening for incoming connections on 0.0.0.0:<port>" is its own log line),
# UDP is only ever outbound (discovery, the Soulseek server connection), and there
# is no second "obfuscated" port to publish: an obfuscated route is a SoulseekQt
# feature slskd does not implement, so a peer that tries one falls back to this
# port rather than needing another.
REACHABLE_NOTE = ("From the internet, TCP {port} is the one port that has to "
                  "reach this host: no UDP port, and no second (obfuscated) "
                  "port — slskd has no obfuscated route.")

NOTE = ("A definite answer about the internet needs a probe from OUTSIDE this "
        "network, which this app does not ship — nothing here contacts a "
        "third-party service. These rows are what can be proven from this "
        "machine: a listener that accepts, a mapping the router itself lists, "
        "and the addresses both depend on.")

# A container cannot forward its own port, and no row above can say so: the
# gateway the probe reaches is Docker's bridge (172.18.x.1), so the automatic
# opening the setting asks for never leaves the container — a UPnP search from
# in here is answered by nothing, and the answer reads like a router that does
# not do UPnP. The router can only forward to the HOST, so the port has to be
# published by compose AND forwarded there by hand.
CONTAINER_NOTE = ("This app runs in a container: the gateway a probe can see "
                  "from here is Docker's bridge, not the home router, so the "
                  "search cannot reach the router by itself. Publish the port "
                  "(docker-compose.yml: ports: \"{port}:{port}\") and forward "
                  "TCP {port} on the ROUTER to the HOST's LAN address — a router "
                  "cannot forward to a container address. Name the router's LAN "
                  "address in the Soulseek settings and the app asks it directly "
                  "instead: both the unicast search and NAT-PMP work from in here "
                  "once the router is named.")

# What each gateway verdict means for the port, whichever side reported it (the
# live read, or `soulseek.portmap_state`, which uses `mlo.portmap`'s own state
# names). A mapping a gateway lists for this machine is the one definite pass; the
# gateway's own refusal is a real failure; everything that means "this could not
# be read" is a warning, because a forward made by hand may still be there.
# `off`/`pending`/`checking`/`client_down` are this app's own states: nothing was
# asked, so nothing is known.
GATEWAY_STATES = {"mapped": "ok", "refused": "fail", "error": "fail",
                  "unsupported": "warn", "no_gateway": "warn",
                  "off": "unknown", "pending": "unknown", "checking": "unknown",
                  "client_down": "unknown"}

# The CGNAT range carriers put subscribers behind (RFC 6598). An address in it is
# the one situation no port mapping can ever fix, so it is named as itself.
CARRIER_NET = ipaddress.ip_network("100.64.0.0/10")

# The mapping states that are a verdict FROM a gateway (the rest of
# `GATEWAY_STATES` are this app's own notes about its own process).
_GATEWAY_WORDS = ("mapped", "refused", "no_gateway", "unsupported", "error")


def _row(cid, label, state, detail, proves, cannot):
    """One check as the endpoint reports it.

    `proves` and `cannot` travel WITH the row on purpose: `state` is only what the
    UI colours, and this pair is what keeps a green row from being read as more
    than it is."""
    return {"id": cid, "label": label, "state": state, "detail": detail,
            "proves": proves, "cannot": cannot}


def _joined(parts):
    """The non-empty sentences, as one line."""
    return " ".join(" ".join(str(p).split()) for p in parts if str(p or "").strip())


def _connect(host, port, timeout):
    """A real TCP connection -> (accepted, why it was not)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True, ""
    except OSError as e:
        return False, (_plain(e.strerror) or _plain(str(e))
                       or f"no answer within {timeout}s")
    finally:
        sock.close()


def _plain(text):
    """An OS error as the phrase a person would read ("connection refused")."""
    return " ".join(str(text or "").split()).lower()


def _wan_kind(address):
    """What a stated WAN address is -> "public"/"carrier"/"private" ("" if not one)."""
    try:
        ip = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return ""
    if ip.version == 4 and ip in CARRIER_NET:
        return "carrier"
    return "private" if ip.is_private else "public"


# --------------------------------------------------------------------------- #
# The listener here
# --------------------------------------------------------------------------- #
def _listen_check(state):
    """What this machine does with the port, from the app's own measurement.

    `state` is `soulseek.port_status_payload()`'s: the connect and the bind test
    are made THERE, once, for the port chip the page already shows — asking twice
    would only produce two answers that can disagree."""
    port = int(state.get("listen_port") or 0)
    proves = ("that something on this machine accepts a TCP connection on this "
              "port. A router mapping to a port nothing listens on forwards "
              "nothing.")
    cannot = ("whose socket it is — the app cannot see which process holds it — "
              "nor anything about the internet side.")
    label = f"Listening on 127.0.0.1:{port}"
    own = _joined([state.get("error"), state.get("slskd_error")])
    if state.get("conflict"):
        # Another program holds the Soulseek port: slskd cannot use it, so peers
        # reach that program and nothing of this app's.
        return _row("listen", label, "fail", f"{state['conflict']}.", proves, cannot)
    if state.get("listening"):
        return _row("listen", label, "ok",
                    _joined([f"a TCP connection to 127.0.0.1:{port} was accepted, "
                             f"and {state.get('holder') or 'something'} holds the "
                             f"port — with slskd answering on its web port that is "
                             f"the daemon's listener.", own]), proves, cannot)
    if state.get("bindable"):
        return _row("listen", label, "fail",
                    _joined([f"nothing accepts a connection on 127.0.0.1:{port} "
                             f"and this machine can bind the port, so nothing is "
                             f"listening on it at all: peers have nothing to "
                             f"reach.", own]), proves, cannot)
    # Not listening AND not bindable: the bind test is what tells this apart from
    # the case above — somebody else holds the port.
    return _row("listen", label, "fail",
                _joined([f"nothing accepts a connection on 127.0.0.1:{port}, and "
                         f"the port cannot be bound here either, so another process "
                         f"holds it (or a socket from an earlier run is still "
                         f"there).", own]), proves, cannot)


# --------------------------------------------------------------------------- #
# The host's own publish line
# --------------------------------------------------------------------------- #
def _container():
    """Whether this process runs in a container.

    Its own seam, so the suite can put a probe in a container without one: the
    payload's `container` flag, the port check and the sharing audit all have to
    answer this the same way for the same process."""
    try:
        from server.auth import in_container
        return bool(in_container())
    except Exception:
        return False


def _served_port(cfg=None):
    """The port this app is served on — the control the publish row is read against.

    `MLO_SERVER_PORT` seeds `server_port` at startup (server/main.py), and the
    compose file publishes THAT number to the host, so a connection to the
    container's gateway on it proves this Docker does hand published ports back
    into the container. A host that maps the web port to some other number makes
    the control read as unpublished too — which is exactly why a refusal on the
    listen port is only ever called a missing publish line when the control was
    ACCEPTED (see `_publish_check`)."""
    candidates = [(os.environ.get("MLO_SERVER_PORT") or "").strip(),
                  (cfg or {}).get("server_port"), DEFAULT_WEB_PORT]
    for value in candidates:
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return DEFAULT_WEB_PORT


def _publish_check(state, cfg=None, container=None, gateway=""):
    """Whether the HOST publishes the very port the daemon listens on.

    The R279 rule ("one port, one number") is what keeps the compose file and the
    daemon from disagreeing by construction, but until now nothing MEASURED it: no
    container can read the host's port list, so a publish line that was missing
    (or on another number) looked exactly like "your router has no UPnP" — the
    owner was sent to the router for a mistake in the compose file, or to the
    compose file while the publish was right. One measurement IS available from in
    here: this container's gateway. A port the host publishes is accepted there
    (the host's proxy/DNAT hands the connection back to the container); one it does
    not publish is refused. The app's own served port is the control, so that
    refusal is only read as a missing publish line on a Docker that demonstrably
    does hand published ports back — otherwise the row says so instead of guessing.

    `state` is `soulseek.port_status_payload()`'s, so this row and the listen row
    describe the same port at the same moment; `container`/`gateway` default to
    this install's own (the sharing audit passes the state it already carries)."""
    port = int(state.get("listen_port") or 0)
    proves = ("that the number slskd holds and the number the host publishes are "
              "the same one: from inside a container a published port is accepted "
              "on the container's gateway, while an unpublished one is refused.")
    cannot = ("whether anything in front of this host forwards it to the internet "
              "— the gateway a container can see is Docker's bridge, never the "
              "owner's router, and this app ships no probe from outside.")
    label = f"Published by the host: TCP {port}"
    reachable = REACHABLE_NOTE.format(port=port)
    if container is None:
        container = bool(state.get("container", _container()))
    if not container:
        return _row("publish", label, "unknown",
                    _joined(["not a container, so no publish line is involved: "
                             "the daemon holds this port on this machine "
                             "itself.", reachable]), proves, cannot)
    if not gateway:
        from mlo import portmap
        try:
            gateway = portmap.default_gateway()
        except Exception:
            gateway = ""
    if not gateway:
        return _row("publish", label, "warn",
                    _joined(["the container's gateway address could not be read "
                             "from here, so the host's own port list could not be "
                             "read either — `docker port <container>` on the host "
                             "says which number it publishes.", reachable]),
                    proves, cannot)
    if not state.get("listening"):
        # A refusal here would say nothing: nothing accepts on the port inside the
        # container either, so a published port reads refused on the gateway too.
        return _row("publish", label, "unknown",
                    _joined([f"nothing accepts on TCP {port} inside the container, "
                             f"so what the host publishes cannot be told apart "
                             f"from a closed listener — the listener row above is "
                             f"the one that matters.", reachable]), proves, cannot)
    accepted, why = _connect(gateway, port, LOCAL_TIMEOUT)
    if accepted:
        return _row("publish", label, "ok",
                    _joined([f"the host publishes TCP {port} to this container: a "
                             f"connection to the container's gateway "
                             f"({gateway}:{port}) was accepted, which is what a "
                             f"published port looks like from in here — so the "
                             f"compose line is not the problem, and whatever is "
                             f"left is in front of the host.", reachable]),
                    proves, cannot)
    control = _served_port(cfg)
    control_ok, control_why = _connect(gateway, control, LOCAL_TIMEOUT)
    if control_ok:
        return _row("publish", label, "fail",
                    _joined([f"the host does not publish TCP {port}: a connection "
                             f"to the container's gateway ({gateway}:{port}) was "
                             f"not accepted ({why}) while the same gateway "
                             f"accepted the app's own port {control} — this "
                             f"Docker does hand published ports back into the "
                             f"container, and this number is missing from its "
                             f"publish list or published as another number.",
                             f"docker-compose.yml has to publish the number slskd "
                             f"listens on: ports: \"{port}:{port}\", with "
                             f"MLO_SOULSEEK_LISTEN_PORT (when it is set) naming "
                             f"that same number.", reachable]), proves, cannot)
    return _row("publish", label, "warn",
                _joined([f"the container's gateway ({gateway}) accepted neither "
                         f"TCP {port} ({why}) nor the app's own port {control} "
                         f"({control_why}), so this host does not hand published "
                         f"ports back into the container and its publish list "
                         f"cannot be read from here — `docker port <container>` on "
                         f"the host says which number it publishes.",
                         reachable]), proves, cannot)


# --------------------------------------------------------------------------- #
# The router's mapping
# --------------------------------------------------------------------------- #
def _lease_note(stored):
    """The lease sentence for the app's stored mapping ("" when none was stated).

    A gateway that granted a lease drops the entry when it runs out, so a lease
    that has already passed means the entry the app confirmed may be gone."""
    expires = float(stored.get("expires_at") or 0.0)
    if not expires:
        return ""
    left = expires - time.time()
    if left > 0:
        return f"the lease the gateway granted runs out in {int(left)}s."
    return ("the lease the gateway granted has run out, so the entry it confirmed "
            "may be gone until the app asks for it again.")


def _mapping_check(port, read, stored):
    """What the router holds for the port, from the live read and the stored verdict.

    The live read is what the gateway says NOW; the stored result is what this app
    was told when it last asked — and for a NAT-PMP gateway that is the only word
    that will ever exist, since NAT-PMP cannot be asked what it holds."""
    proves = ("that the router itself lists a forwarding entry for this port, "
              "which is what a peer's connection to the public address has to "
              "reach.")
    cannot = ("anything past the router: the carrier's own filtering, and whether "
              "the WAN address the router states is the one the internet sees.")
    label = f"Router mapping for port {port}"
    detail = str(read.get("detail") or "").strip()
    # Only a state a GATEWAY reported is a word from the router. The app's own
    # process notes ("never asked yet", "asking now", "no client") are not, and
    # quoting them as the router's answer would invent a source.
    was = ""
    if stored.get("state") in _GATEWAY_WORDS and stored.get("detail"):
        was = f"the app's own last request said: {stored['detail']}"
    state = GATEWAY_STATES.get(str(read.get("state") or ""), "unknown")
    if state == "ok":
        return _row("mapping", label, "ok",
                    _joined([detail, was, _lease_note(stored)]), proves, cannot)
    if stored.get("enabled") is False:
        # The app asks the router for nothing when the setting is off, so the
        # absence of an entry says nothing: a forward made on the router's own
        # page may not be listed as an entry anything here can read.
        return _row("mapping", label, "unknown",
                    _joined(["automatic port opening is off in this app, so it "
                             "never asks the router for a mapping — a forward "
                             "made by hand can exist without being readable "
                             "here.", detail]), proves, cannot)
    stored_state = str(stored.get("state") or "")
    if state == "fail" or stored_state in ("refused", "error"):
        return _row("mapping", label, "fail", _joined([detail, was]), proves, cannot)
    if stored_state == "mapped":
        # This app got a mapping confirmed once and cannot read it back now (a
        # NAT-PMP gateway, or a UPnP device that refuses the read): reported as
        # what it is, not as open.
        return _row("mapping", label, "warn",
                    _joined([detail,
                             f"the app's own mapping request was confirmed: "
                             f"{stored.get('detail')}",
                             _lease_note(stored)]), proves, cannot)
    return _row("mapping", label, state,
                _joined([detail, was, _lease_note(stored)]), proves, cannot)


# --------------------------------------------------------------------------- #
# The addresses
# --------------------------------------------------------------------------- #
def _address_check(read, stored, lan, container=False):
    """The addresses the port's reachability depends on.

    Two shapes decide whether ANY mapping can work: where the mapping points (a
    forward to another host forwards nothing here), and what address the gateway
    states for itself — a carrier-grade NAT, or a router that is itself behind
    another router, is the one situation a port mapping can never fix, and it is
    named as that instead of being blamed on a firewall.

    `container` is the shape this check has to be told about, because inside one
    the two addresses are SUPPOSED to differ: the router must forward the port to
    the HOST's address on its own network, while the address this process can see
    for itself is Docker's bridge (measured: the router lists 50000 ->
    192.168.40.62:50000 while the process answers on 172.18.0.3). Reading that as
    "peers would reach another device" is the opposite of the truth — the host
    publishes the port back to this very container — so a mapping that names some
    other address is judged by what the app could measure about it: a read that
    reached that address on the port is a forward that lands on a listener."""
    proves = ("that the addresses this app can read line up: the mapping points "
              "at this machine, and the router states a public address for itself.")
    cannot = ("whether the carrier really routes that address to this router, and "
              "whether the router's own answer is true — its admin page is the "
              "other place to look.")
    mapping_ip = str(read.get("internal_ip") or stored.get("internal_ip") or "").strip()
    wan = str(read.get("external_ip") or stored.get("external_ip") or "").strip()
    bridged = bool(container) and bool(mapping_ip) and bool(lan) and mapping_ip != lan
    facts = []
    if mapping_ip:
        if bridged:
            # Behind a bridge the app cannot state its own address on the
            # router's network at all, so the comparison above is meaningless —
            # only what answered at that address means anything.
            if read.get("state") == "mapped" and read.get("verified"):
                facts.append(("ok",
                              f"the mapping points at {mapping_ip} — the HOST's "
                              f"address on the router's network, which this "
                              f"container cannot see for itself (it answers on "
                              f"{lan}). A connection to {mapping_ip} on the port "
                              f"was accepted, so the forward lands on a listener, "
                              f"and the publish row above that lands on this "
                              f"container carries the port the rest of the way."))
            else:
                facts.append(("unknown",
                              f"the mapping points at {mapping_ip}, and this "
                              f"process answers on {lan} — it runs behind "
                              f"Docker's bridge, so the two cannot be compared "
                              f"from in here. What can be said is what the router "
                              f"lists, and the mapping row above reports that."))
        elif not lan:
            facts.append(("unknown",
                          f"the mapping points at {mapping_ip}, and this machine's "
                          f"own address on that network could not be read, so the "
                          f"two cannot be compared."))
        elif mapping_ip == lan:
            facts.append(("ok", f"the mapping points at this machine ({lan})."))
        else:
            facts.append(("fail",
                          f"the mapping points at {mapping_ip}, but this machine is "
                          f"{lan} on that network — peers would reach another "
                          f"device."))
    elif read.get("state") == "mapped" or stored.get("state") == "mapped":
        facts.append(("unknown", "the gateway did not say which address the "
                                 "mapping points at."))
    kind = _wan_kind(wan)
    if not wan:
        facts.append(("unknown",
                      "no gateway stated its WAN address, so the shape of this "
                      "network could not be read from here."))
    elif kind == "carrier":
        facts.append(("fail",
                      f"the gateway states {wan} as its own WAN address, which is "
                      f"a carrier-grade NAT address (100.64.0.0/10): inbound "
                      f"connections are not routed to this router at all, so no "
                      f"port mapping on it can ever make this port reachable from "
                      f"the internet. Ask the ISP for a public address."))
    elif kind == "private":
        facts.append(("fail",
                      f"the gateway states {wan} as its own WAN address, which is a "
                      f"private address: this router is behind another NAT (double "
                      f"NAT), and the port would have to be forwarded on the OTHER "
                      f"router — only the one this app is behind can be asked."))
    elif kind == "public":
        facts.append(("ok",
                      f"the gateway states {wan} as its own WAN address, which is "
                      f"public, so a mapping there is reachable in principle."))
    else:
        facts.append(("warn",
                      f"the gateway stated its own WAN address as {wan!r}, which "
                      f"is not an address this can read."))
    for want in ("fail", "warn", "unknown", "ok"):
        if any(state == want for state, _text in facts):
            state = want
            break
    return _row("address", "Addresses", state,
                _joined([text for _state, text in facts]), proves, cannot)


# --------------------------------------------------------------------------- #
# The connection from here to the public address
# --------------------------------------------------------------------------- #
def _self_connect_check(port, wan):
    """A TCP connection to the router's WAN address, from this machine.

    The one thing this can never be is proof of closure: a router without NAT
    hairpinning (most home routers) refuses a connection that starts inside to its
    own public address while the port is open to the outside, so a refusal is
    `unknown` — by construction, never `fail`."""
    proves = ("that a connection to the router's WAN address reaches something "
              "which accepts on this port — a forwarding path that works end to "
              "end as far as this machine can see.")
    cannot = ("whether the internet reaches that address: without NAT hairpinning "
              "a router refuses exactly this connection while the port is still "
              "open to the outside, so a refusal here can never be read as "
              "closed.")
    label = f"Self-connect to {wan}:{port}" if wan else "Self-connect"
    if not wan:
        return _row("self-connect", label, "unknown",
                    "no gateway stated a WAN address, so there is no public "
                    "address to try from here.", proves, cannot)
    ok, why = _connect(wan, port, PUBLIC_TIMEOUT)
    if ok:
        return _row("self-connect", label, "ok",
                    f"a TCP connection to {wan}:{port} was accepted from this "
                    f"machine.", proves, cannot)
    return _row("self-connect", label, "unknown",
                f"the connection to {wan}:{port} was not accepted ({why}), which "
                f"is what a router without NAT hairpinning does — it does NOT mean "
                f"the port is closed to the outside.", proves, cannot)


# --------------------------------------------------------------------------- #
# slskd's own answer
# --------------------------------------------------------------------------- #
def _network_check(running, cfg):
    """slskd's own word about the Soulseek network — the strongest signal here.

    Asked only when the daemon answered its web port first: the REST call carries
    the app's own long timeout, and a probe that can hang on a wedged daemon would
    be worse than no probe at all."""
    proves = ("that slskd's connection to the Soulseek network is up, which is "
              "what it needs to answer searches and start downloads.")
    cannot = ("that peers can open a connection TO this machine: a login travels "
              "over the OUTGOING connection, so it says nothing about the listen "
              "port the network sees.")
    label = "Signed in to Soulseek"
    if not running:
        return _row("network", label, "unknown",
                    "slskd is not answering on its web port, so it cannot say "
                    "whether it is on the Soulseek network.", proves, cannot)
    from server import soulseek

    try:
        server = soulseek.server_state(cfg)
    except Exception as e:
        return _row("network", label, "warn",
                    f"slskd answered on its web port but not on its own /server "
                    f"route ({e}).", proves, cannot)
    if not server:
        return _row("network", label, "warn",
                    "slskd reports no server state, which is what a logged-out "
                    "daemon answers.", proves, cannot)
    if server.get("isLoggedIn"):
        return _row("network", label, "ok",
                    "slskd reports it is signed in to the Soulseek network.",
                    proves, cannot)
    why = soulseek.login_error(cfg)
    return _row("network", label, "warn",
                _joined(["slskd is running but not signed in to the Soulseek "
                         "network.",
                         f"its own words: {why}" if why else ""]), proves, cannot)


# --------------------------------------------------------------------------- #
# One payload
# --------------------------------------------------------------------------- #
def _verdict(checks):
    """The one line, out of the rows.

    The worst row reported — `fail`, then `warn` — and otherwise the chain this
    app can actually prove: `ok` only when something accepts on the port AND the
    router lists a mapping for it. The self-connect row is advisory by design (its
    own `cannot` says why), so it can confirm a verdict but never lower one, and a
    row that could not be made leaves the verdict `unknown` rather than a pass."""
    states = {c["id"]: c["state"] for c in checks}
    if "fail" in states.values():
        return "fail"
    if "warn" in states.values():
        return "warn"
    if states.get("listen") == "ok" and states.get("mapping") == "ok":
        return "ok"
    return "unknown"


def port_check(cfg=None):
    """Every check this app can make about the Soulseek listen port, with a verdict.

    `port` is the SAVED listen port (what slskd was configured to hold), and each
    row carries what it proves and what it cannot. Read-only end to end: no
    mapping is added or removed and no lock is taken, so this can be asked while a
    download runs."""
    from mlo import portmap
    from server import soulseek

    cfg = cfg or load_config()
    state = soulseek.port_status_payload(cfg)
    port = int(state.get("listen_port") or DEFAULT_PORT)
    running = soulseek.client_running(cfg)
    stored = state.get("mapping") or {}
    # The router the user named wins over everything else: a container's own
    # gateway is Docker's bridge, and this app's search AND its NAT-PMP both work
    # against the real router the moment it is named (measured on an OpenWRT IGD
    # that ignores multicast and answers unicast).
    gateway = (str(cfg.get("soulseek_router_ip") or "").strip()
               or str(stored.get("gateway") or "")
               or portmap.default_gateway())
    lan = portmap.local_ip(gateway)
    read = portmap.read_port(port, ip=lan, gateway=gateway, timeout=GATEWAY_TIMEOUT)
    wan = str(read.get("external_ip") or stored.get("external_ip") or "").strip()
    container = _container()
    checks = [
        _listen_check(state),
        # The host's own publish line, read from in here (a container only): the
        # half of R279 that no number in the config can prove by agreeing. It is
        # asked about THIS container's gateway (Docker's bridge), always — the
        # router a user named for the mapping is a different machine, and a
        # probe sent there measures the router, not the host's port list.
        _publish_check(state, cfg, container=container, gateway=""),
        _mapping_check(port, read, stored),
        _address_check(read, stored, lan, container=container),
        _self_connect_check(port, wan),
        _network_check(running, cfg),
    ]
    if container:
        # On the row whose silence is otherwise read as "my router has no UPnP":
        # in a container that verdict is about Docker's bridge, not the router.
        # (The publish row above is the one that DOES read the host's side, and it
        # says so itself — it is not a second place to repeat this.)
        for row in checks:
            if row["id"] == "mapping":
                row["detail"] = _joined([row["detail"], CONTAINER_NOTE.format(port=port)])
    verdict = _verdict(checks)
    return {"ok": verdict == "ok", "port": port, "checks": checks,
            "verdict": verdict, "note": NOTE, "container": container,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
