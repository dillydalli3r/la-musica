"""Can peers reach this app's Soulseek listen port — the probe behind "Test port".

The honest scope first, because it decides every state below: a definite "open to
the internet" answer needs a probe from OUTSIDE this network, and this app ships
none (nothing here contacts a third-party service). What IS available is a chain
of things that can be observed from this machine, and every row says which link
of it that row is:

  * listen        a real TCP connection to 127.0.0.1:<port> plus — when nothing
                  answers — the bind test that separates "slskd is not listening"
                  from "another process holds the port";
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

NOTE = ("A definite answer about the internet needs a probe from OUTSIDE this "
        "network, which this app does not ship — nothing here contacts a "
        "third-party service. These rows are what can be proven from this "
        "machine: a listener that accepts, a mapping the router itself lists, "
        "and the addresses both depend on.")

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
def _address_check(read, stored, lan):
    """The addresses the port's reachability depends on.

    Two shapes decide whether ANY mapping can work: where the mapping points (a
    forward to another host forwards nothing here), and what address the gateway
    states for itself — a carrier-grade NAT, or a router that is itself behind
    another router, is the one situation a port mapping can never fix, and it is
    named as that instead of being blamed on a firewall."""
    proves = ("that the addresses this app can read line up: the mapping points "
              "at this machine, and the router states a public address for itself.")
    cannot = ("whether the carrier really routes that address to this router, and "
              "whether the router's own answer is true — its admin page is the "
              "other place to look.")
    mapping_ip = str(read.get("internal_ip") or stored.get("internal_ip") or "").strip()
    wan = str(read.get("external_ip") or stored.get("external_ip") or "").strip()
    facts = []
    if mapping_ip:
        if not lan:
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
    gateway = str(stored.get("gateway") or "") or portmap.default_gateway()
    lan = portmap.local_ip(gateway)
    read = portmap.read_port(port, ip=lan, gateway=gateway, timeout=GATEWAY_TIMEOUT)
    wan = str(read.get("external_ip") or stored.get("external_ip") or "").strip()
    checks = [
        _listen_check(state),
        _mapping_check(port, read, stored),
        _address_check(read, stored, lan),
        _self_connect_check(port, wan),
        _network_check(running, cfg),
    ]
    verdict = _verdict(checks)
    return {"ok": verdict == "ok", "port": port, "checks": checks,
            "verdict": verdict, "note": NOTE,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
