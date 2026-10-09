#!/usr/bin/env python3
"""Audit the port-mapping module against local gateways that answer (or refuse).

A port mapping is a claim about the outside world, and the whole point of
mlo/portmap.py is that the claim is TRUE: this app does not report a forwarded
port because it asked for one, only because a gateway answered the request that
makes it. So every protocol is exercised against a test double on loopback — a
fake SSDP responder, a fake IGD answering the SOAP actions, and a fake NAT-PMP
gateway — and the asserts below check the bytes ON THE WIRE (the M-SEARCH's
search target, the SOAP action and every argument it carries, the NAT-PMP
opcode and port encoding) as well as the refusals: an IGD that answers discovery
and then refuses the mapping has to come back as refused, with the gateway's own
reason, and can never come back as mapped.

Nothing here touches the real network beyond 127.0.0.1: the doubles bind
ephemeral loops, the gateway address is passed in, and the one function that
would read this machine's routing table is only called by the code paths the
doubles replace. The last section then drives the CLIENT side — server.soulseek's
lifecycle — with slskd and the router both stubbed, because a mapping that is
made but never replaced, or a start that waits on a router, is a bug no protocol
test would catch.

Run:  python tools/test_portmap.py
"""
import http.server
import os
import socket
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import portmap

PORT = 50000
SERVICE = "urn:schemas-upnp-org:service:WANIPConnection:1"
DEVICE_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"


def localname(tag):
    return str(tag).rsplit("}", 1)[-1]


def ssdp_reply(location, st, sender):
    return ("HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {location}\r\n"
            f"ST: {st}\r\n"
            "USN: uuid:fake-igd::urn:schemas-upnp-org:device:"
            "InternetGatewayDevice:1\r\n"
            "SERVER: FakeIGD/1.0 UPnP/1.0\r\n"
            "\r\n").encode("ascii")


class FakeSSDP:
    """A UPnP device that answers M-SEARCH on a loopback UDP port.

    Every query it receives is recorded verbatim, which is how the M-SEARCH's
    own shape (start line, HOST, MAN, MX, ST) is asserted."""

    def __init__(self, location, answer=(DEVICE_ST,), silent=False):
        self.location = location
        self.answer = tuple(answer)
        self.silent = silent
        self.queries = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, sender = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            text = data.decode("utf-8", "replace")
            self.queries.append(text)
            headers = {}
            for line in text.splitlines()[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            st = headers.get("st", "")
            if not self.silent and st in self.answer:
                try:
                    self.sock.sendto(ssdp_reply(self.location, st, sender), sender)
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        self.sock.close()


# The description an IGD serves: the control URL is RELATIVE on purpose, so the
# resolution against the description's own URL is exercised too.
DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>Fake IGD</friendlyName>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <serviceList>
              <service>
                <serviceType>{service}</serviceType>
                <controlURL>/ctl/IPConn</controlURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>
""".format(service=SERVICE)

# ...and one with no WAN service at all: a device that answers discovery and
# cannot map anything, which must be its own reported state.
NO_SERVICE_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Not an IGD</friendlyName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/ctl/AVT</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"""


def envelope(action, args):
    """An IGD's SOAP RESPONSE body: the action element with `Response` on the
    end, which is the only shape the client reads the answer out of."""
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    return ('<?xml version="1.0"?><s:Envelope '
            'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="{SERVICE}">{body}</u:{action}Response>'
            "</s:Body></s:Envelope>")


def fault(code, description):
    return ('<?xml version="1.0"?><s:Envelope '
            'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            "<s:Fault><faultcode>s:Client</faultcode>"
            "<faultstring>UPnPError</faultstring><detail>"
            '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f"<errorCode>{code}</errorCode>"
            f"<errorDescription>{description}</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>")


class _IGDHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        igd = self.server.igd
        igd.gets.append(self.path)
        self._send(200, igd.description)

    def do_POST(self):
        igd = self.server.igd
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        soap_action = self.headers.get("SOAPAction", "")
        action = soap_action.split("#")[-1].strip('"')
        # parse the action element's arguments exactly as the gateway does
        args = {}
        try:
            root = ET.fromstring(raw)
            for el in root.iter():
                if localname(el.tag) == action:
                    args = {localname(c.tag): (c.text or "") for c in el}
        except ET.ParseError:
            args = {}
        igd.calls.append({"path": self.path, "soap_action": soap_action,
                          "content_type": self.headers.get("Content-Type", ""),
                          "action": action, "args": args, "raw": raw})
        status, text = igd.respond(action, args, raw)
        self._send(status, text)


class FakeIGD:
    """An IGD's SOAP control endpoint on loopback (description + actions)."""

    def __init__(self, description=DESCRIPTION, add_refusal=None,
                 specific="echo", external="203.0.113.9", delete_refusal=None):
        self.description = description
        self.add_refusal = add_refusal      # (errorCode, "description") or ("http", 500)
        self.specific = specific            # echo | other_host | none | unsupported | disabled
        self.external = external
        self.delete_refusal = delete_refusal
        self.calls = []                     # every POST, with its parsed args
        self.gets = []
        self.mapping = None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _IGDHandler)
        self.server.igd = self
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05),
            daemon=True)
        self.thread.start()

    def url(self, path="/rootDesc.xml"):
        return f"http://127.0.0.1:{self.port}{path}"

    def respond(self, action, args, raw):
        if action == "GetExternalIPAddress":
            if not self.external:
                return 500, fault(501, "ActionFailed")
            return 200, envelope("GetExternalIPAddress",
                                 {"NewExternalIPAddress": self.external})
        if action == "AddPortMapping":
            if self.add_refusal:
                kind = self.add_refusal[0]
                if kind == "http":
                    return self.add_refusal[1], ""
                return 500, fault(*self.add_refusal)
            self.mapping = dict(args)
            return 200, envelope("AddPortMapping", {})
        if action == "DeletePortMapping":
            if self.delete_refusal:
                return 500, fault(*self.delete_refusal)
            self.mapping = None
            return 200, envelope("DeletePortMapping", {})
        if action == "GetSpecificPortMappingEntry":
            if self.specific == "unsupported":
                return 500, fault(401, "Invalid Action")
            if self.specific == "none":
                return 500, fault(714, "NoSuchEntryInArray")
            if not self.mapping:
                return 500, fault(714, "NoSuchEntryInArray")
            host = self.mapping.get("NewInternalClient", "")
            if self.specific == "other_host":
                host = "192.168.40.99"
            enabled = "0" if self.specific == "disabled" else "1"
            return 200, envelope("GetSpecificPortMappingEntry", {
                "NewInternalPort": self.mapping.get("NewInternalPort", ""),
                "NewInternalClient": host,
                "NewEnabled": enabled,
                "NewPortMappingDescription":
                    self.mapping.get("NewPortMappingDescription", ""),
            })
        return 500, fault(401, "Invalid Action")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakePMP:
    """A NAT-PMP gateway (RFC 6886) on a loopback UDP port.

    Every request's opcode and port encoding is recorded, so the wire format —
    not just the outcome — is asserted."""

    def __init__(self, external="203.0.113.7", result=0, granted=3600,
                 silent=False, mapped=None):
        self.external = external
        self.result = result
        self.granted = granted
        self.silent = silent
        self.mapped = mapped
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        epoch = int(time.time())
        while not self._stop.is_set():
            try:
                data, sender = self.sock.recvfrom(64)
            except (socket.timeout, OSError):
                continue
            record = {"version": data[0], "opcode": data[1], "raw": data}
            if data[1] in (1, 2):
                version, opcode, _reserved, internal, external, lifetime = \
                    struct.unpack("!BBHHHI", data[:12])
                record.update({"internal": internal, "external": external,
                               "lifetime": lifetime, "protocol": opcode})
            self.requests.append(record)
            if self.silent:
                continue
            if data[1] == 0:
                reply = struct.pack("!BBHI4s", 0, 128, self.result, epoch,
                                    socket.inet_aton(self.external))
            elif data[1] in (1, 2):
                life = 0 if record.get("lifetime") == 0 else self.granted
                mapped = self.mapped or record.get("external", 0)
                reply = struct.pack("!BBHIHHI", 0, 128 + data[1], self.result,
                                    epoch, record.get("internal", 0), mapped, life)
            else:
                reply = struct.pack("!BBHI", 0, 128 + data[1], 5, epoch)
            try:
                self.sock.sendto(reply, sender)
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self.sock.close()


def closed_udp_port():
    """A loopback UDP port that nothing answers on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def closed_tcp_port():
    """A loopback TCP port that nothing accepts on (bind, learn it, release it)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def igd_with(targets=(DEVICE_ST,), **over):
    """A fake IGD plus the SSDP responder that points at its description."""
    igd = FakeIGD(**over)
    ssdp = FakeSSDP(igd.url(), answer=targets)
    return igd, ssdp


def upnp_open(igd, ssdp, port=PORT, **over):
    """upnp_open() aimed at the loopback doubles."""
    return portmap.upnp_open(port, timeout=0.3, ssdp_addr="127.0.0.1",
                             ssdp_port=ssdp.port, **over)


def pmp_open(pmp, port=PORT, **over):
    """natpmp_open() aimed at the loopback double."""
    return portmap.natpmp_open(port, gateway="127.0.0.1", timeout=0.2,
                               attempts=2, pmp_port=pmp.port, **over)


# --------------------------------------------------------------------------- #
# 1) the M-SEARCH and the device description
# --------------------------------------------------------------------------- #
igd, ssdp = igd_with()
replies = portmap.ssdp_search(DEVICE_ST, timeout=0.3, addr="127.0.0.1",
                              port=ssdp.port)
assert len(replies) == 1, replies
assert replies[0]["location"] == igd.url(), replies
assert replies[0]["from"] == "127.0.0.1", replies
assert "FakeIGD" in replies[0]["server"], replies

query = ssdp.queries[0]
lines = query.split("\r\n")
assert lines[0] == "M-SEARCH * HTTP/1.1", lines[0]
assert f"HOST: 127.0.0.1:{ssdp.port}" in lines, lines
assert 'MAN: "ssdp:discover"' in lines, lines
assert "MX: 1" in lines, lines
assert f"ST: {DEVICE_ST}" in lines, lines
assert query.endswith("\r\n\r\n"), repr(query[-6:])
# ...and the header names are the uppercase ones the UPnP spec mandates
assert "HOST:" in query and "host:" not in query

# the discovery that finds the WAN service: the relative control URL is
# resolved against the description's own URL, and the device is named
found, errors = portmap.discover_igd(timeout=0.3, ssdp_addr="127.0.0.1",
                                     ssdp_port=ssdp.port)
assert errors == [], errors
assert found["service"] == SERVICE, found
assert found["control_url"] == igd.url("/ctl/IPConn"), found
assert found["device"] == "Fake IGD", found

# a device that answers discovery but describes no WAN service is reported as
# such (its own state), not as a mapping and not as a silent no-gateway
base = f"http://127.0.0.1:{igd.port}"
not_an_igd = FakeIGD(description=NO_SERVICE_DESCRIPTION)
ssdp2 = FakeSSDP(not_an_igd.url())
found2, errors2 = portmap.discover_igd(timeout=0.3, ssdp_addr="127.0.0.1",
                                       ssdp_port=ssdp2.port)
assert found2 is None, found2
assert any("no WAN port-mapping service" in e for e in errors2), errors2

# nothing answers at all
silent = FakeSSDP(igd.url(), silent=True)
found3, errors3 = portmap.discover_igd(timeout=0.2, ssdp_addr="127.0.0.1",
                                       ssdp_port=silent.port)
assert found3 is None and errors3 == [], (found3, errors3)
silent.close()
not_an_igd.close()
ssdp2.close()

# every search target is tried when the device search goes unanswered: a router
# that only answers a service search must still be found
igd3 = FakeIGD()
ssdp3 = FakeSSDP(igd3.url(), answer=("urn:schemas-upnp-org:service:WANPPPConnection:1",))
found4, errors4 = portmap.discover_igd(timeout=0.2, ssdp_addr="127.0.0.1",
                                       ssdp_port=ssdp3.port)
assert found4 and found4["service"] == SERVICE, (found4, errors4)
targets = [line.split("ST: ")[1] for q in ssdp3.queries for line in q.split("\r\n")
           if line.startswith("ST: ")]
assert targets == list(portmap.SEARCH_TARGETS), targets
igd3.close()
ssdp3.close()

print("ok  the SSDP search carries the device/service search targets and the "
      "device description resolves to the WAN control URL")


# --------------------------------------------------------------------------- #
# 1b) the router that ignores the multicast group: found by UNICAST
# --------------------------------------------------------------------------- #
# Measured on the OpenWRT IGD this was built against: four patient M-SEARCHes to
# 239.255.255.250:1900 (both device targets, both service types) got ZERO replies,
# while the same search sent straight at 192.168.40.1:1900 answered at once. A
# process behind Docker's bridge can never reach that address by discovery — it
# has to be NAMED — so a named gateway is searched by unicast after the multicast
# pass. The doubles stand in for both halves: the address the multicast pass uses
# (127.0.0.2, where nothing answers) and the same port reached by unicast at
# 127.0.0.1, where the device does answer.

# an IGD v2 answers the v2 DEVICE target, and it is searched: this shape of
# router answered only that one target
V2_DEVICE_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:2"
assert V2_DEVICE_ST in portmap.SEARCH_TARGETS, portmap.SEARCH_TARGETS
assert (portmap.SEARCH_TARGETS.index(DEVICE_ST)
        < portmap.SEARCH_TARGETS.index(V2_DEVICE_ST)), portmap.SEARCH_TARGETS
v2_igd = FakeIGD()
v2_ssdp = FakeSSDP(v2_igd.url(), answer=(V2_DEVICE_ST,))
found_v2, errors_v2 = portmap.discover_igd(timeout=0.2, ssdp_addr="127.0.0.1",
                                           ssdp_port=v2_ssdp.port)
assert found_v2 and found_v2["service"] == SERVICE, (found_v2, errors_v2)
assert found_v2["device"] == "Fake IGD", found_v2
targets_v2 = [line.split("ST: ")[1] for q in v2_ssdp.queries
              for line in q.split("\r\n") if line.startswith("ST: ")]
# the v1 target first (which this device does not answer), then the v2 one — the
# search stops at the first target that yields a usable device
assert targets_v2 == [DEVICE_ST, V2_DEVICE_ST], targets_v2
v2_igd.close()
v2_ssdp.close()

# the unicast-only router: named -> found; the very same search without the
# address -> nothing, which is the report the panel used to give
unicast_igd = FakeIGD()
unicast_ssdp = FakeSSDP(unicast_igd.url())
found_u, errors_u = portmap.discover_igd(timeout=0.2, ssdp_addr="127.0.0.2",
                                         ssdp_port=unicast_ssdp.port,
                                         gateways=["127.0.0.1"])
assert found_u and found_u["service"] == SERVICE, (found_u, errors_u)
assert found_u["from"] == "127.0.0.1", found_u
assert errors_u == [], errors_u
# the search that found it went to the NAMED address, not to the multicast group
assert len(unicast_ssdp.queries) == 1, unicast_ssdp.queries
assert f"HOST: 127.0.0.1:{unicast_ssdp.port}" in unicast_ssdp.queries[0], \
    unicast_ssdp.queries[0]
silent_u, errors_none = portmap.discover_igd(timeout=0.2, ssdp_addr="127.0.0.2",
                                             ssdp_port=unicast_ssdp.port)
assert silent_u is None and errors_none == [], (silent_u, errors_none)
assert len(unicast_ssdp.queries) == 1, unicast_ssdp.queries

# ...and through the public entry points, where `gateway=` is the only difference
named = portmap.upnp_open(PORT, gateway="127.0.0.1", timeout=0.25,
                          ssdp_addr="127.0.0.2", ssdp_port=unicast_ssdp.port)
assert named["state"] == "mapped" and named["ok"] is True, named
assert named["internal_ip"] == "127.0.0.1", named
unnamed = portmap.upnp_open(PORT, timeout=0.25, ssdp_addr="127.0.0.2",
                            ssdp_port=unicast_ssdp.port)
assert unnamed["state"] == "no_gateway" and unnamed["ok"] is False, unnamed
assert "no device answered the UPnP search" in unnamed["detail"], unnamed
# the close path searches the named router the same way, or a mapping this app
# made could never be removed again
closed = portmap.close_port(PORT, gateway="127.0.0.1", timeout=0.25,
                            ssdp_addr="127.0.0.2", ssdp_port=unicast_ssdp.port,
                            pmp_port=closed_udp_port())
assert closed["state"] == "released" and closed["method"] == "upnp", closed
assert unicast_igd.mapping is None, unicast_igd.mapping
unicast_igd.close()
unicast_ssdp.close()

# a named router whose answers cannot be USED is reported against that address,
# so a reader can act on it (and not against the multicast address it never was)
dead_ssdp = FakeSSDP(f"http://127.0.0.1:{closed_udp_port()}/rootDesc.xml")
found_d, errors_d = portmap.discover_igd(timeout=0.2, ssdp_addr="127.0.0.2",
                                         ssdp_port=dead_ssdp.port,
                                         gateways=[" 127.0.0.1 ", "127.0.0.1", ""])
assert found_d is None, found_d
assert any(e.startswith("127.0.0.1: ") and "could not be read" in e
           for e in errors_d), errors_d
dead_ssdp.close()

# a named router that stays silent is NOT an error — nobody answered, so the
# verdict is `no_gateway` — but the detail names every address that was asked
quiet_ssdp = FakeSSDP("http://127.0.0.1:1/rootDesc.xml", silent=True)
quiet = portmap.upnp_open(PORT, gateway="127.0.0.1", timeout=0.2,
                          ssdp_addr="127.0.0.2", ssdp_port=quiet_ssdp.port)
assert quiet["state"] == "no_gateway" and quiet["ok"] is False, quiet
assert "no device answered the UPnP search" in quiet["detail"], quiet
assert f"127.0.0.1:{quiet_ssdp.port}" in quiet["detail"], quiet
quiet_ssdp.close()

# the addresses a caller passes are cleaned before anything is sent: a blank, a
# padded repeat and the same address twice are ONE address, four searches
counting = FakeSSDP("http://127.0.0.1:1/rootDesc.xml", silent=True)
portmap.discover_igd(timeout=0.1, ssdp_addr="127.0.0.2", ssdp_port=counting.port,
                     gateways=["127.0.0.1", " 127.0.0.1", "", "127.0.0.1"])
assert len(counting.queries) == len(portmap.SEARCH_TARGETS), counting.queries
counting.close()

print("ok  a router that ignores the multicast group is found through the "
      "UNICAST search of its named address, and the v2 device target is searched")


# --------------------------------------------------------------------------- #
# 2) AddPortMapping: what the router is actually told
# --------------------------------------------------------------------------- #
igd, ssdp = igd_with()
result = upnp_open(igd, ssdp)
assert result["state"] == "mapped" and result["ok"] is True, result
assert result["method"] == "upnp", result
assert result["verified"] is True, result
assert result["external_ip"] == "203.0.113.9", result
assert result["internal_ip"] == "127.0.0.1", result
assert result["listen_port"] == PORT, result
assert "Fake IGD" in result["detail"], result["detail"]

get_calls = igd.gets
assert get_calls == ["/rootDesc.xml"], get_calls
add = [c for c in igd.calls if c["action"] == "AddPortMapping"]
assert len(add) == 1, igd.calls
assert add[0]["path"] == "/ctl/IPConn", add[0]
assert add[0]["soap_action"] == f'"{SERVICE}#AddPortMapping"', add[0]
assert add[0]["content_type"].startswith("text/xml"), add[0]
assert add[0]["args"] == {
    "NewRemoteHost": "",
    "NewExternalPort": str(PORT),
    "NewProtocol": "TCP",
    "NewInternalPort": str(PORT),
    "NewInternalClient": "127.0.0.1",
    "NewEnabled": "1",
    "NewPortMappingDescription": portmap.DEFAULT_DESCRIPTION,
    # 0 = no lease: the mapping stays until it is removed
    "NewLeaseDuration": "0",
}, add[0]["args"]
assert add[0]["raw"].startswith('<?xml version="1.0"?>'), add[0]["raw"]
assert SERVICE in add[0]["raw"], add[0]["raw"]
# the entry was read back before the mapping was called made
specific = [c for c in igd.calls if c["action"] == "GetSpecificPortMappingEntry"]
assert len(specific) == 1, igd.calls
assert specific[0]["args"]["NewExternalPort"] == str(PORT), specific[0]
assert specific[0]["args"]["NewProtocol"] == "TCP", specific[0]
igd.close()
ssdp.close()

# the router answers the add and then does NOT list the entry: that is a refusal
# with the gateway's own words, never a mapping
igd, ssdp = igd_with(specific="none")
result = upnp_open(igd, ssdp)
assert result["state"] == "refused" and result["ok"] is False, result
assert "NoSuchEntryInArray" in result["detail"], result
assert "714" in result["detail"], result
igd.close()
ssdp.close()

# the router lists the port as another host's: also a refusal, and a useful one
igd, ssdp = igd_with(specific="other_host")
result = upnp_open(igd, ssdp)
assert result["state"] == "refused" and result["ok"] is False, result
assert "192.168.40.99" in result["detail"] and "another device" in result["detail"], result
igd.close()
ssdp.close()

# the router lists the entry but disabled
igd, ssdp = igd_with(specific="disabled")
result = upnp_open(igd, ssdp)
assert result["state"] == "refused" and result["ok"] is False, result
assert "disabled" in result["detail"], result
igd.close()
ssdp.close()

# the router cannot read an entry back (Invalid Action): the accepted add stands,
# and the result says the mapping could NOT be verified
igd, ssdp = igd_with(specific="unsupported")
result = upnp_open(igd, ssdp)
assert result["state"] == "mapped" and result["ok"] is True, result
assert result["verified"] is False, result
assert "does not support reading it back" in result["detail"], result
assert "Invalid Action" in result["detail"], result
igd.close()
ssdp.close()

# the router refuses the SOAP action outright (718 ConflictInMappingEntry)
igd, ssdp = igd_with(add_refusal=(718, "ConflictInMappingEntry"))
result = upnp_open(igd, ssdp)
assert result["state"] == "refused" and result["ok"] is False, result
assert result["method"] == "upnp", result
assert "ConflictInMappingEntry" in result["detail"], result
assert "718" in result["detail"], result
assert result["verified"] is False, result
assert igd.mapping is None, igd.mapping
# ...and no read-back was even attempted: the add never succeeded
assert not [c for c in igd.calls if c["action"] == "GetSpecificPortMappingEntry"], igd.calls
igd.close()
ssdp.close()

# a control endpoint that answers with a bare HTTP error and no SOAP fault is
# refused too, with the status in the reason — never mapped
igd, ssdp = igd_with(add_refusal=("http", 500))
result = upnp_open(igd, ssdp)
assert result["state"] == "refused" and result["ok"] is False, result
assert "HTTP 500" in result["detail"], result
igd.close()
ssdp.close()

# an IGD whose description cannot be read at all: reported as unsupported, with
# the reason, and never as a mapping
igd = FakeIGD()
ssdp = FakeSSDP(f"http://127.0.0.1:{closed_udp_port()}/rootDesc.xml")
result = upnp_open(igd, ssdp)
assert result["state"] == "unsupported" and result["ok"] is False, result
assert "could not be read" in result["detail"], result
igd.close()
ssdp.close()

# nothing on the network answers discovery at all
silent = FakeSSDP("http://127.0.0.1:1/rootDesc.xml", silent=True)
result = portmap.upnp_open(PORT, timeout=0.2, ssdp_addr="127.0.0.1",
                           ssdp_port=silent.port)
assert result["state"] == "no_gateway" and result["ok"] is False, result
assert result["attempts"][0]["answered"] is False, result
silent.close()

print("ok  UPnP AddPortMapping carries every argument the IGD schema declares, "
      "and every refusal path reports the gateway's own reason as refused")


# --------------------------------------------------------------------------- #
# 2b) the container hazard: never name an address the router cannot dial
# --------------------------------------------------------------------------- #
# A UPnP mapping names the internal client, and this process would name the
# address its own side of Docker's bridge holds (172.18.0.3 while the router is
# on 192.168.40.0/24). The router cannot dial that, so the mapping would be a
# forward to nowhere that still reads as success. `local_ip` is the one call that
# decides which address this process would name for itself, so it is the call
# these cases replace — the doubles' `from` is loopback, and the bridge is the
# address the process would name, never the address the router answers from.
real_local_ip = portmap.local_ip
portmap.local_ip = lambda gateway="": "172.18.0.3"
try:
    igd, ssdp = igd_with()
    upnp_only = upnp_open(igd, ssdp)
    assert upnp_only["state"] == "unsupported", upnp_only
    assert upnp_only["ok"] is False, upnp_only
    assert upnp_only["internal_ip"] == "172.18.0.3", upnp_only
    # NO AddPortMapping was sent — nothing was forwarded to nowhere
    assert not [c for c in igd.calls if c["action"] == "AddPortMapping"], igd.calls
    assert igd.mapping is None, igd.mapping
    assert "AddPortMapping was not sent" in upnp_only["detail"], upnp_only
    assert "NAT-PMP" in upnp_only["detail"], upnp_only
    assert "172.18.0.3" in upnp_only["detail"], upnp_only

    # ...and the entry point maps the port the way that works from a bridge:
    # NAT-PMP carries no internal address at all (the gateway maps to whoever
    # asked), so it is the method that survives Docker's NAT
    pmp = FakePMP()
    result = portmap.open_port(PORT, gateway="127.0.0.1", timeout=0.25,
                               ssdp_addr="127.0.0.1", ssdp_port=ssdp.port,
                               pmp_port=pmp.port)
    assert result["state"] == "mapped" and result["ok"] is True, result
    assert result["method"] == "natpmp" and result["verified"] is True, result
    assert not [c for c in igd.calls if c["action"] == "AddPortMapping"], igd.calls
    assert [t["method"] for t in result["tried"]] == ["upnp", "natpmp"], \
        result["tried"]
    assert result["tried"][0]["state"] == "unsupported", result["tried"]
    assert "172.18.0.3" in result["tried"][0]["detail"], result["tried"]
    # the NAT-PMP request itself names no internal address, which is why it can
    # be honoured where the UPnP one could not
    assert [r["opcode"] for r in pmp.requests] == [0, 2], pmp.requests
    pmp.close()

    # an ip the CALLER names is trusted: the guard is about the address this
    # process works out for itself, never about overriding a caller that knows a
    # route this process cannot see
    igd2, ssdp2 = igd_with()
    trusted = upnp_open(igd2, ssdp2, ip="172.18.0.3")
    assert trusted["state"] == "mapped" and trusted["ok"] is True, trusted
    assert trusted["internal_ip"] == "172.18.0.3", trusted
    add = [c for c in igd2.calls if c["action"] == "AddPortMapping"]
    assert len(add) == 1, igd2.calls
    assert add[0]["args"]["NewInternalClient"] == "172.18.0.3", add[0]["args"]
    igd2.close()
    ssdp2.close()
    igd.close()
    ssdp.close()
finally:
    portmap.local_ip = real_local_ip

print("ok  a bridged process never sends the AddPortMapping its router could not "
      "use, and NAT-PMP is the method that maps the port instead")


# --------------------------------------------------------------------------- #
# 3) NAT-PMP: the wire format and the refusals
# --------------------------------------------------------------------------- #
pmp = FakePMP()
result = pmp_open(pmp)
assert result["state"] == "mapped" and result["ok"] is True, result
assert result["method"] == "natpmp" and result["verified"] is True, result
assert result["external_ip"] == "203.0.113.7", result
assert result["gateway"] == "127.0.0.1", result
# the request names no internal address (NAT-PMP maps "whoever asked"), so the
# app states the interface it left by itself
assert result["internal_ip"] == "127.0.0.1", result
assert result["expires_at"] > time.time(), result
assert [r["opcode"] for r in pmp.requests] == [0, 2], pmp.requests
# the external-address request is two bytes: version 0, opcode 0
assert pmp.requests[0]["raw"] == b"\x00\x00", pmp.requests[0]["raw"]
# the map request: version 0, opcode 2 (TCP), reserved 0, internal port, the
# SAME port requested externally, and the lifetime — 12 bytes, big-endian
assert pmp.requests[1]["raw"] == struct.pack("!BBHHHI", 0, 2, 0, PORT, PORT,
                                              portmap.PMP_LIFETIME), \
    pmp.requests[1]["raw"]
assert pmp.requests[1]["protocol"] == 2 and pmp.requests[1]["internal"] == PORT
assert pmp.requests[1]["external"] == PORT
assert pmp.requests[1]["lifetime"] == portmap.PMP_LIFETIME

# a mapped port the gateway chose differently is reported as the gateway stated
pmp.close()
pmp = FakePMP(mapped=51000)
result = pmp_open(pmp)
assert result["state"] == "mapped" and "51000" in result["detail"], result
pmp.close()

# the gateway refuses with a result code: spelled out, and not a mapping
pmp = FakePMP(result=2)
result = pmp_open(pmp)
assert result["state"] == "refused" and result["ok"] is False, result
assert "not authorized" in result["detail"], result
assert result["attempts"][0]["detail"] == "NAT-PMP not authorized, or the " \
                                         "mapping was refused (result 2)", result
pmp.close()

# ...and the same mapping that was refused is still reported as refused after a
# longer lifetime is asked for (the encoding is the caller's, the verdict the
# gateway's)
pmp = FakePMP(result=4)
result = pmp_open(pmp, lifetime=600)
assert result["state"] == "refused" and "out of resources" in result["detail"], result
assert pmp.requests[1]["lifetime"] == 600, pmp.requests[1]
pmp.close()

# a gateway that never answers: no_gateway, never mapped, and it said so after
# retransmitting rather than after one lost datagram
dead = closed_udp_port()
result = portmap.natpmp_open(PORT, gateway="127.0.0.1", timeout=0.15, attempts=2,
                             pmp_port=dead)
assert result["state"] == "no_gateway" and result["ok"] is False, result
assert result["attempts"][0]["answered"] is False, result

# a gateway that answers nothing but is there (silent double): same verdict
silent_pmp = FakePMP(silent=True)
result = pmp_open(silent_pmp)
assert result["state"] == "no_gateway" and result["ok"] is False, result
silent_pmp.close()

# no gateway address at all (a machine with no default route): nothing is sent,
# and that too is reported as no_gateway
result = portmap.natpmp_open(PORT, gateway="0.0.0.0", timeout=0.15, attempts=1,
                             pmp_port=closed_udp_port())
assert result["state"] == "no_gateway", result

# removing a mapping is the SAME request with a zero lifetime
pmp = FakePMP()
pmp_open(pmp)
result = portmap.natpmp_close(PORT, gateway="127.0.0.1", timeout=0.2,
                              attempts=2, pmp_port=pmp.port)
assert result["state"] == "released" and result["ok"] is True, result
assert result["method"] == "natpmp", result
assert pmp.requests[-1]["opcode"] == 2 and pmp.requests[-1]["lifetime"] == 0, \
    pmp.requests[-1]
pmp.close()

print("ok  NAT-PMP asks for the external address then maps the TCP port with the "
      "gateway's own result code reported on refusal")


# --------------------------------------------------------------------------- #
# 4) the one entry point: UPnP first, then NAT-PMP — truthfully
# --------------------------------------------------------------------------- #
# UPnP maps it: NAT-PMP is not even asked
igd, ssdp = igd_with()
pmp = FakePMP()
result = portmap.open_port(PORT, ip="127.0.0.1", gateway="127.0.0.1", timeout=0.3,
                           ssdp_addr="127.0.0.1", ssdp_port=ssdp.port,
                           pmp_port=pmp.port)
assert result["state"] == "mapped" and result["method"] == "upnp", result
assert pmp.requests == [], pmp.requests
assert len(result["tried"]) == 1, result["tried"]
igd.close()
ssdp.close()
pmp.close()

# UPnP answers discovery and REFUSES the mapping, NAT-PMP has no gateway: the
# refusal is the verdict, with the router's reason — and the NAT-PMP attempt is
# still reported
igd, ssdp = igd_with(add_refusal=(718, "ConflictInMappingEntry"))
pmp = FakePMP(silent=True)
result = portmap.open_port(PORT, ip="127.0.0.1", gateway="127.0.0.1", timeout=0.2,
                           ssdp_addr="127.0.0.1", ssdp_port=ssdp.port,
                           pmp_port=pmp.port)
assert result["state"] == "refused" and result["ok"] is False, result
assert "ConflictInMappingEntry" in result["detail"], result
assert {t["method"] for t in result["tried"]} == {"upnp", "natpmp"}, result["tried"]
assert [t["state"] for t in result["tried"]] == ["refused", "no_gateway"], result["tried"]
assert result["tried"][1]["method"] == "natpmp", result["tried"]
igd.close()
ssdp.close()
pmp.close()

# UPnP answers discovery but cannot map (no WAN service), NAT-PMP maps it: the
# mapping is reported with the method that made it, and the UPnP failure is kept
not_an_igd = FakeIGD(description=NO_SERVICE_DESCRIPTION)
ssdp = FakeSSDP(not_an_igd.url())
pmp = FakePMP()
result = portmap.open_port(PORT, ip="127.0.0.1", gateway="127.0.0.1", timeout=0.2,
                           ssdp_addr="127.0.0.1", ssdp_port=ssdp.port,
                           pmp_port=pmp.port)
assert result["state"] == "mapped" and result["method"] == "natpmp", result
assert result["ok"] is True and result["verified"] is True, result
assert result["external_ip"] == "203.0.113.7", result
assert len(result["tried"]) == 2, result["tried"]
assert result["tried"][0]["method"] == "upnp" and result["tried"][0]["state"] == "unsupported", result["tried"]
assert "no WAN port-mapping service" in result["tried"][0]["detail"], result["tried"]
not_an_igd.close()
ssdp.close()
pmp.close()

# NOTHING answers: no_gateway, not ok, and no invented external address
silent = FakeSSDP("http://127.0.0.1:1/rootDesc.xml", silent=True)
pmp = FakePMP(silent=True)
result = portmap.open_port(PORT, ip="127.0.0.1", gateway="127.0.0.1", timeout=0.15,
                           ssdp_addr="127.0.0.1", ssdp_port=silent.port,
                           pmp_port=pmp.port)
assert result["state"] == "no_gateway" and result["ok"] is False, result
assert result["external_ip"] == "" and result["verified"] is False, result
assert result["method"] == "upnp", result
assert len(result["tried"]) == 2, result["tried"]
silent.close()
pmp.close()

# neither method enabled is its own state, so a caller cannot read "off" as "failed"
result = portmap.open_port(PORT, methods=())
assert result["state"] == "omitted" and result["ok"] is False, result

# the closing entry point: UPnP DeletePortMapping first
igd, ssdp = igd_with()
pmp_open_result = portmap.open_port(PORT, ip="127.0.0.1", gateway="127.0.0.1", timeout=0.3,
                                    ssdp_addr="127.0.0.1", ssdp_port=ssdp.port,
                                    pmp_port=closed_udp_port())
assert pmp_open_result["state"] == "mapped", pmp_open_result
result = portmap.close_port(PORT, gateway="127.0.0.1", timeout=0.3, ssdp_addr="127.0.0.1",
                            ssdp_port=ssdp.port, pmp_port=closed_udp_port())
assert result["state"] == "released" and result["ok"] is True, result
assert result["method"] == "upnp", result
delete = [c for c in igd.calls if c["action"] == "DeletePortMapping"]
assert len(delete) == 1, igd.calls
assert delete[0]["args"] == {"NewRemoteHost": "", "NewExternalPort": str(PORT),
                             "NewProtocol": "TCP"}, delete[0]["args"]
assert delete[0]["soap_action"] == f'"{SERVICE}#DeletePortMapping"', delete[0]
assert igd.mapping is None, igd.mapping
igd.close()
ssdp.close()

# a mapping that is already gone (714) is released, not an error
igd, ssdp = igd_with(delete_refusal=(714, "NoSuchEntryInArray"))
result = portmap.close_port(PORT, gateway="127.0.0.1", timeout=0.3, ssdp_addr="127.0.0.1",
                            ssdp_port=ssdp.port, pmp_port=closed_udp_port())
assert result["state"] == "released" and result["ok"] is True, result
assert "NoSuchEntryInArray" in result["detail"], result
igd.close()
ssdp.close()

# a gateway that refuses the delete is refused — a mapping this app cannot
# remove is not reported as removed
igd, ssdp = igd_with(delete_refusal=(501, "ActionFailed"))
pmp = FakePMP(silent=True)
result = portmap.close_port(PORT, gateway="127.0.0.1", timeout=0.2, ssdp_addr="127.0.0.1",
                            ssdp_port=ssdp.port, pmp_port=pmp.port)
assert result["state"] == "refused" and result["ok"] is False, result
assert "ActionFailed" in result["detail"], result
assert {t["method"] for t in result["tried"]} == {"upnp", "natpmp"}, result["tried"]
igd.close()
ssdp.close()
pmp.close()

# the routing table this machine really has is readable (the one call the doubles
# replace), and a machine without one yields the honest empty answer
gateway = portmap.default_gateway()
assert gateway == "" or gateway.count(".") == 3, gateway
assert portmap._pmp_result_text(3) == "network failure"
assert portmap._pmp_result_text(99) == "result code 99"
assert portmap.local_ip("not-an-address") == "", "a bad address yields no interface"

print("ok  the entry point tries UPnP then NAT-PMP, keeps every attempt, and "
      "reports a refusal as refused")


# --------------------------------------------------------------------------- #
# 5) the client's lifecycle: start, port change, switch off
# --------------------------------------------------------------------------- #
# The mapping is only worth anything if it follows the CLIENT: made when slskd
# starts, replaced when the listen port changes, removed when the setting is
# turned off — and never allowed to delay a start. slskd itself is stubbed here
# (its start path is driven through the ADOPT branch, which spawns nothing), and
# the router is stubbed through mlo.portmap's own entry points.
import tempfile

import server.soulseek as soulseek

events = []


def fake_open(port, **kw):
    events.append(("open", port))
    return portmap._out("mapped", ok=True, method="upnp", port=port, ip="127.0.0.1",
                        external="203.0.113.9", verified=True,
                        detail=f"the gateway lists external port {port} -> "
                               f"127.0.0.1:{port}")


def fake_close(port, **kw):
    events.append(("close", port))
    return portmap._out("released", ok=True, method="upnp", port=port,
                        detail=f"the mapping of port {port} was removed")


real_open, real_close = portmap.open_port, portmap.close_port
# The reconciler READS the router before it asks it to map (soulseek.portmap_sync
# reads the entry back first, so a mapping already in place is not replaced), so
# the read is stubbed with the rest of the router: this section is about the
# mapping following the client, and a real read would spend the whole multicast
# timeout on every pass. `unsupported` is a gateway that ANSWERED and cannot be
# read back — the verdict the read-before-ask order falls through to `open_port`
# from. (`no_gateway` is the other half, and it does NOT fall through: see the
# two cases at the end of this section.)
real_read = portmap.read_port
portmap.read_port = lambda port, **kw: portmap._out(
    "unsupported", method="upnp", port=port,
    detail="the gateway accepted the mapping; it does not support reading it back")
portmap.open_port, portmap.close_port = fake_open, fake_close
real_running, real_exe, real_write = (soulseek.client_running, soulseek.slskd_exe,
                                      soulseek.write_config)
real_owner, real_dirs = soulseek.instance_owner, soulseek._uses_our_downloads
real_is_running = soulseek.is_running
real_load_config = soulseek.load_config
soulseek.client_running = lambda cfg=None: True  # a client that is up


def reset():
    """A fresh process's mapping state, and an empty event log."""
    events.clear()
    with soulseek._PORTMAP_LOCK:
        soulseek._PORTMAP.update({"result": None, "port": 0, "checked_at": 0.0,
                                  "in_flight": False, "reason": ""})


CFG_UP = {"music_folder": os.path.join(tempfile.gettempdir(), "mlo-portmap-music"),
          "soulseek_listen_port": 50000, "soulseek_upnp": True}
CFG_OFF = dict(CFG_UP, soulseek_upnp=False)

# the switch is off: the router is never asked, and the state says so
reset()
state = soulseek.portmap_sync(CFG_OFF, reason="test")
assert state["state"] == "off" and state["enabled"] is False, state
assert events == [], events
assert "port opening is off" in state["detail"].lower(), state

# ...and a mapping this app made is REMOVED when it is turned off
reset()
soulseek.portmap_sync(CFG_UP, reason="test")
assert events == [("open", 50000)], events
assert soulseek.portmap_state(CFG_UP)["state"] == "mapped"
state = soulseek.portmap_sync(CFG_OFF, reason="test")
assert events == [("open", 50000), ("close", 50000)], events
assert state["state"] == "off" and state["enabled"] is False, state

# asking twice for the same port does not ask the router again — the reconciler
# runs on a timer, and a mapping that is already right is not re-made
reset()
soulseek.portmap_sync(CFG_UP, reason="test")
events.clear()
soulseek.portmap_sync(CFG_UP, reason="watch")
assert events == [], events
state = soulseek.portmap_state(CFG_UP)
assert state["state"] == "mapped", state
assert state["external_ip"] == "203.0.113.9", state
assert state["verified"] is True, state
assert state["method"] == "upnp", state
# ...but a forced sync (the client just started) asks again
soulseek.portmap_sync(CFG_UP, reason="test", force=True)
assert events == [("open", 50000)], events

# the listen port CHANGED: the old forward points at a port nothing listens on
# any more, so it is removed before the new one is made
events.clear()
new = dict(CFG_UP, soulseek_listen_port=50100)
state = soulseek.portmap_sync(new, reason="test")
assert events == [("close", 50000), ("open", 50100)], events
assert state["state"] == "mapped" and state["listen_port"] == 50100, state
assert state["mapped_port"] == 50100, state

# a lease the gateway GRANTED is not re-asked on every pass — a NAT-PMP mapping
# is re-asked at half the lease it was actually given, not every 20 seconds
reset()
soulseek.portmap_sync(CFG_UP, reason="test")
with soulseek._PORTMAP_LOCK:
    soulseek._PORTMAP["result"] = dict(soulseek._PORTMAP["result"],
                                       expires_at=time.time() + 3600)
    soulseek._PORTMAP["checked_at"] = time.time()
events.clear()
soulseek.portmap_sync(CFG_UP, reason="watch")
assert events == [], events
with soulseek._PORTMAP_LOCK:
    # a 3600s lease granted 3590s ago: it is due to be refreshed
    soulseek._PORTMAP["result"] = dict(soulseek._PORTMAP["result"],
                                       expires_at=time.time() + 10)
    soulseek._PORTMAP["checked_at"] = time.time() - 3590
events.clear()
soulseek.portmap_sync(CFG_UP, reason="watch")
assert events == [("open", 50000)], events

# a release with the switch still ON is not "off" (the feature is enabled) and
# not "mapped" (nobody confirmed a forward): it is a port with no mapping yet
reset()
with soulseek._PORTMAP_LOCK:
    soulseek._PORTMAP.update({
        "result": portmap._out("released", ok=True, method="upnp", port=50000,
                               detail="the mapping of port 50000 was removed"),
        "port": 50000, "checked_at": time.time()})
state = soulseek.portmap_state(CFG_UP)
assert state["state"] == "pending" and state["enabled"] is True, state
assert "was removed" in state["detail"], state
assert "has not been made yet" in state["detail"], state

# a router that refuses: the state carries the refusal and its reason, and the
# client is recorded as RUNNING regardless — a port mapping never gates slskd
def refusing_open(port, **kw):
    events.append(("open", port))
    return portmap._out("refused", method="upnp", port=port,
                        detail=f"Fake IGD refused to forward port {port}: "
                               f"ConflictInMappingEntry (UPnP error 718)",
                        attempts=[portmap._attempt(
                            "upnp", True, False,
                            "Fake IGD: ConflictInMappingEntry (UPnP error 718)")])


portmap.open_port = refusing_open
try:
    reset()
    state = soulseek.portmap_sync(CFG_UP, reason="test")
    assert state["state"] == "refused" and state["in_flight"] is False, state
    assert "ConflictInMappingEntry" in state["detail"], state
    assert state["tried"][0]["state"] == "refused", state["tried"]
    assert state["state"] != "mapped" and state["verified"] is False, state
finally:
    portmap.open_port = fake_open

# the read finds the port ALREADY forwarded: the router is not asked to map it
# again. This is the whole point of asking first — a re-map REPLACES the entry,
# and on the router this was measured on, replacing a standing forward with a
# two-hour NAT-PMP lease and then dropping that lease closed a port that had
# been open all along.
portmap.read_port = lambda port, **kw: portmap._out(
    "mapped", ok=True, method="upnp", port=port, ip="203.0.113.9",
    external="198.51.100.7", verified=True,
    detail=f"the gateway lists external port {port} -> 203.0.113.9:{port}")
try:
    reset()
    state = soulseek.portmap_sync(CFG_UP, reason="test")
    assert events == [], events
    assert state["state"] == "mapped" and state["verified"] is True, state
    assert state["external_ip"] == "198.51.100.7", state
    assert state["gateway"] == "", state  # the read stated none; nothing invented
finally:
    portmap.read_port = lambda port, **kw: portmap._out(
        "unsupported", method="upnp", port=port,
        detail="the gateway accepted the mapping; it does not support reading it back")

# and when NOTHING answers — neither the UPnP search nor NAT-PMP — the read's own
# verdict is what is stored: `open_port` would put the same two questions to the
# same silence and pay a second wait for the answer, on every pass of a watcher
# that runs every 20 seconds.
portmap.read_port = lambda port, **kw: portmap._out(
    "no_gateway", method="upnp", port=port,
    detail="no device answered the UPnP search on 239.255.255.250:1900, nor the "
           "M-SEARCH sent straight to 192.168.40.1:1900")
try:
    reset()
    state = soulseek.portmap_sync(CFG_UP, reason="test")
    assert events == [], events
    assert state["state"] == "no_gateway" and state["enabled"] is True, state
    assert "192.168.40.1:1900" in state["detail"], state
finally:
    portmap.read_port = lambda port, **kw: portmap._out(
        "unsupported", method="upnp", port=port,
        detail="the gateway accepted the mapping; it does not support reading it back")

# nothing is mapped while the client is down: there is no listener to forward to
reset()
soulseek.client_running = lambda cfg=None: False
try:
    state = soulseek.portmap_sync(CFG_UP, reason="test")
    assert events == [], events
    assert state["state"] == "client_down", state
finally:
    soulseek.client_running = lambda cfg=None: True

# the reconciler runs on a timer (config saves never come through this module),
# and its first pass reconciles immediately rather than waiting out the interval
soulseek.load_config = lambda: CFG_UP
reset()
soulseek._PORTMAP_POLL = 3600.0
soulseek._ensure_portmap_watcher()
first = soulseek._PORTMAP_WATCH["thread"]
soulseek._ensure_portmap_watcher()
assert soulseek._PORTMAP_WATCH["thread"] is first, "the watcher was started twice"
assert first.daemon is True, "a watcher that outlives the app would hold it open"
soulseek._PORTMAP_WATCH["stop"].set()
first.join(timeout=5)
assert not first.is_alive(), "the watcher did not stop"
soulseek._PORTMAP_WATCH["thread"] = None
assert events == [("open", 50000)], events

# starting the client asks for the mapping IN THE BACKGROUND: slskd's own start
# must never wait on a router. The ADOPT branch is used because it spawns
# nothing — it is the branch a backend restart takes.
reset()
soulseek.is_running = lambda: False
soulseek.slskd_exe = lambda: os.path.join(tempfile.mkdtemp(prefix="mlo-portmap-"),
                                          "slskd.exe")
soulseek.write_config = lambda cfg=None: "fake-key"
soulseek.instance_owner = lambda cfg=None: (True, "tester", "")
soulseek._uses_our_downloads = lambda cfg: True
# the reconciler is asserted separately above, so its threading is stubbed here
# and only its being started by start() is recorded
watcher_starts = []
real_watcher_starter = soulseek._ensure_portmap_watcher
soulseek._ensure_portmap_watcher = lambda: watcher_starts.append(1)


def slow_open(port, **kw):
    time.sleep(1.5)
    events.append(("open", port))
    return portmap._out("mapped", ok=True, method="upnp", port=port,
                        detail="slow router")


portmap.open_port = slow_open
try:
    started = time.monotonic()
    ok, message = soulseek.start(CFG_UP)
    elapsed = time.monotonic() - started
    assert ok is True, (ok, message)
    assert message == "adopted already-running slskd", message
    assert elapsed < 1.0, f"start() waited {elapsed:.2f}s on the router"
    assert watcher_starts == [1], watcher_starts
    deadline = time.time() + 6.0
    while not events and time.time() < deadline:
        time.sleep(0.05)
    assert events == [("open", 50000)], events
finally:
    portmap.open_port = fake_open

# ...and a client started with the setting OFF does not ask at all
reset()
try:
    soulseek.start(CFG_OFF)
    time.sleep(0.5)
    assert events == [], events
    assert soulseek.portmap_state(CFG_OFF)["state"] == "off"
finally:
    portmap.open_port, portmap.close_port = real_open, real_close
    portmap.read_port = real_read
    (soulseek.client_running, soulseek.slskd_exe,
     soulseek.write_config) = (real_running, real_exe, real_write)
    soulseek.instance_owner, soulseek._uses_our_downloads = real_owner, real_dirs
    soulseek.is_running = real_is_running
    soulseek.load_config = real_load_config
    soulseek._ensure_portmap_watcher = real_watcher_starter
    soulseek._PORTMAP_POLL = 20.0

print("ok  the mapping follows the client: made at start, replaced when the port "
      "changes, removed when the switch goes off, never blocking the start")

# --------------------------------------------------------------------------- #
# The read must SURVIVE a network with no UPnP device and a gateway that will
# not talk NAT-PMP — the ordinary case on a router with UPnP switched off, and
# the one the "Test port" probe exists to explain. `read_port` called
# `pmp_external_address(gw, ..., pmp_port=...)`, a keyword that function does not
# take, so the whole read raised `TypeError` and the probe the README points
# users at answered 500 on EVERY install, gateway or no gateway. Nothing below
# is stubbed: real discovery against a closed port, the real NAT-PMP client
# against a gateway that answers nothing.
no_gateway = portmap.read_port(
    50000, gateway="127.0.0.1", timeout=0.3, ssdp_addr="127.0.0.1",
    ssdp_port=closed_udp_port(), pmp_port=closed_udp_port())
assert isinstance(no_gateway, dict), no_gateway
assert no_gateway["listen_port"] == 50000, no_gateway
assert no_gateway["state"] in ("no_gateway", "unsupported"), no_gateway
assert any("no device answered" in " ".join(map(str, row.values()))
           for row in no_gateway["attempts"]), no_gateway
print(" ok  a read with no UPnP device answers a report instead of raising")

# --------------------------------------------------------------------------- #
# Reading an entry back from behind a bridge
# --------------------------------------------------------------------------- #
# The entry a router holds names the address it forwards to, and from behind
# Docker's bridge that is the HOST — an address this process cannot claim to be
# its own (it sees 172.18.0.3). So an entry pointing elsewhere is not "another
# device holds it": it is judged by the one fact available, whether that address
# answers on the port.
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(8)
live_port = listener.getsockname()[1]


def drain():
    """Accept and drop connections, so the accept queue never fills: a full
    backlog stops completing connects, which would read as "nothing listening"
    and turn the accept below into a spurious refusal."""
    while True:
        try:
            conn, _peer = listener.accept()
        except OSError:
            return
        conn.close()


threading.Thread(target=drain, daemon=True).start()

bridge_igd = FakeIGD()
bridge_ssdp = FakeSSDP(bridge_igd.url())
bridge_igd.mapping = {"NewInternalClient": "127.0.0.1",
                      "NewInternalPort": str(live_port), "NewEnabled": "1",
                      "NewPortMappingDescription": "the host's forward"}
bridged = portmap.read_port(live_port, ip="172.18.0.3", gateway="127.0.0.1",
                            timeout=0.25, ssdp_addr="127.0.0.1",
                            ssdp_port=bridge_ssdp.port, pmp_port=closed_udp_port())
assert bridged["state"] == "mapped" and bridged["ok"] is True, bridged
assert bridged["verified"] is True, bridged
assert "another device" not in bridged["detail"], bridged
assert f"127.0.0.1:{live_port}" in bridged["detail"], bridged
assert "accepts a connection" in bridged["detail"], bridged
assert "behind Docker's bridge" in bridged["detail"], bridged

# ...and `read_port` hands the caller's `gateway` to its own search: the same
# read, with the multicast coordinates pointing where nothing answers, is found
# only because the router was named
read_named = portmap.read_port(live_port, ip="172.18.0.3", gateway="127.0.0.1",
                               timeout=0.25, ssdp_addr="127.0.0.2",
                               ssdp_port=bridge_ssdp.port,
                               pmp_port=closed_udp_port())
assert read_named["state"] == "mapped", read_named

# the same entry with nothing answering at that address: the gateway forwards the
# port to a place that does not accept, which is a refusal and not a mapping
dead_port = closed_tcp_port()
bridge_igd.mapping = {"NewInternalClient": "127.0.0.1",
                      "NewInternalPort": str(dead_port), "NewEnabled": "1",
                      "NewPortMappingDescription": "gone"}
bridged_dead = portmap.read_port(dead_port, ip="172.18.0.3",
                                 gateway="127.0.0.1", timeout=0.25,
                                 ssdp_addr="127.0.0.2",
                                 ssdp_port=bridge_ssdp.port,
                                 pmp_port=closed_udp_port())
assert bridged_dead["state"] == "refused" and bridged_dead["ok"] is False, \
    bridged_dead
assert bridged_dead["verified"] is False, bridged_dead
assert f"127.0.0.1:{dead_port}" in bridged_dead["detail"], bridged_dead
assert "nothing there accepts" in bridged_dead["detail"], bridged_dead

# ...and the same entry read with NO bridge is what it always was: the entry
# names an address other than this machine's, so another device holds the port
bridge_igd.mapping = {"NewInternalClient": "192.168.40.5",
                      "NewInternalPort": str(live_port), "NewEnabled": "1",
                      "NewPortMappingDescription": "another host"}
plain = portmap.read_port(live_port, ip="127.0.0.9", gateway="127.0.0.1",
                          timeout=0.25, ssdp_addr="127.0.0.2",
                          ssdp_port=bridge_ssdp.port,
                          pmp_port=closed_udp_port())
assert plain["state"] == "refused" and plain["ok"] is False, plain
assert "192.168.40.5" in plain["detail"], plain
assert "another device" in plain["detail"], plain
listener.close()
bridge_igd.close()
bridge_ssdp.close()

print(" ok  an entry read from behind a bridge is judged by what answers at its "
      "address; on this machine's own network it is still another device holding "
      "the port")

print("ok")
