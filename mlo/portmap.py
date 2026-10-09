"""Ask the router to open the Soulseek listen port: UPnP IGD, then NAT-PMP.

slskd has no port-mapping option of its own — upstream closed the UPnP/NAT-PMP
request unimplemented and its Options has no such property — so a client whose
router does not forward the listen port looks offline to the Soulseek network
while everything else about it is fine. This module asks the gateway itself, in
the two ways residential routers actually answer:

* UPnP IGD: an SSDP M-SEARCH for the InternetGatewayDevice (v1 and v2, and, for
  the routers that only answer a service search, for the WANIPConnection/
  WANPPPConnection service types), the device description behind the reply, then
  SOAP ``AddPortMapping`` / ``DeletePortMapping`` / ``GetExternalIPAddress`` /
  ``GetSpecificPortMappingEntry`` on the control URL that description names.
  The multicast group is only half of a search: a router that drops it still
  answers an M-SEARCH sent straight at its own LAN address, so a gateway named
  by the caller is searched by unicast as well (see `discover_igd`).
* NAT-PMP (RFC 6886): UDP to the gateway's port 5351 — external-address request,
  then a TCP map request (opcode 2) with a lifetime, or the same request with a
  zero lifetime to remove the mapping.

Every result is structured and truthful: what was attempted, which method
answered, the external address when a gateway *stated* one, and the gateway's
OWN error text when it refused (a SOAP fault's description/code, or NAT-PMP's
result code spelled out). A mapping is only ever reported as made when the
gateway answered the request that makes it — and, wherever the gateway supports
reading the entry back, when the entry it lists is the one that was asked for.
Two things this code refuses to do, both of them lies a bridged container
invites: send an ``AddPortMapping`` naming an address the router cannot dial —
this process would name its own side of Docker's bridge, and NAT-PMP, which maps
"whoever asked", is the method that applies there — and read an entry pointing at
another address as "another device holds it" when the process is behind that
bridge and cannot say which address on the router's network is its own.

Stdlib only: socket/struct/ctypes for the wire and the routing tables,
urllib.request for the device description and the SOAP posts, xml.etree for the
SOAP/description bodies.
"""
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

# SSDP: the UPnP discovery address every IGD listens on.
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
# NAT-PMP's well-known port on the gateway (RFC 6886 §3).
NATPMP_PORT = 5351
PMP_VERSION = 0
# The device search first: a router answering it sends its ROOT description,
# which names the WAN connection service. The two service searches are the
# fallback for the IGDs that only reply to a service-type M-SEARCH. Both device
# versions are searched: an IGD v2 (the routers that also speak NAT-PMP) answers
# only its own target on some firmware, and it was measured to answer the v2
# device M-SEARCH while never answering the v1 one.
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
# WANIPConnection before WANPPPConnection: IP is what an Ethernet/Wi-Fi WAN
# speaks, PPP is the DSL/PPPoE-style uplink.
WAN_SERVICES = ("WANIPConnection", "WANPPPConnection")

# What the router's port-forwarding page shows as the mapping's name.
DEFAULT_DESCRIPTION = "la musica Soulseek"

# The router may cap the mapping's lifetime (UPnP: NewLeaseDuration; NAT-PMP:
# the granted lifetime) and nothing tells us when it does, so a caller re-asks
# well before the lease it granted runs out (see the `expires_at` field).
PMP_LIFETIME = 7200

# Bodies are small; these caps keep a hostile or broken responder from feeding
# this process unbounded data.
_MAX_DESCRIPTION_BYTES = 512 * 1024
_MAX_SOAP_BYTES = 64 * 1024

# NAT-PMP result codes (RFC 6886 §3.5) — the protocol carries no text of its
# own, so the code is spelled out for the user.
PMP_RESULTS = {
    1: "unsupported version",
    2: "not authorized, or the mapping was refused",
    3: "network failure",
    4: "out of resources",
    5: "unsupported opcode",
}

# `state` values every public function reports:
#   mapped      a gateway answered the request that makes the mapping
#   released    a gateway confirmed the mapping is gone
#   refused     a gateway answered and refused — `detail` carries its words
#   no_gateway  no gateway answered on either method
#   unsupported a device answered but offers no WAN port mapping at all
#   omitted     that method was not tried
#   error       the local side failed (no address to map to)


class GatewayRefused(Exception):
    """The gateway answered and refused; `str(e)` is the gateway's own words."""


class GatewayUnreachable(Exception):
    """Nothing answered, or the answer could not be used; `str(e)` says what."""


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
def _attempt(method, answered, ok, detail):
    """One method's protocol-level try, for the result's `attempts` list."""
    return {"method": method, "answered": bool(answered), "ok": bool(ok),
            "detail": detail}


def _out(state, *, ok=False, method="", port=0, ip="", external="", gateway="",
         verified=False, detail="", attempts=None, expires_at=0.0):
    """The structured result every public function returns.

    `tried` is the per-method verdict the UI prints ("UPnP: refused — …",
    "NAT-PMP: no gateway"); `attempts` is the raw protocol record behind it."""
    return {"state": state, "ok": bool(ok), "method": method,
            "listen_port": int(port or 0), "internal_ip": ip,
            "external_ip": external, "gateway": gateway, "verified": bool(verified),
            "detail": detail, "attempts": list(attempts or []),
            "tried": ([{"method": method, "state": state, "detail": detail}]
                      if method else []),
            "expires_at": float(expires_at or 0.0)}


def _joined(details):
    """The first few non-empty details, as one sentence."""
    seen, out = set(), []
    for text in details:
        text = " ".join(str(text or "").split())
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return " ".join(out[:3])


# --------------------------------------------------------------------------- #
# Addressing
# --------------------------------------------------------------------------- #
def local_ip(gateway=""):
    """This machine's address on the route to *gateway* ("" when there is none).

    A connected UDP socket names the interface the OS would use — no packet is
    ever sent, which is why this works on a machine with no internet. A port
    mapping must name the address the GATEWAY reaches this host on, which is the
    same interface."""
    target = (gateway or "1.1.1.1", 53)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(target)
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def _network24(addr):
    """An IPv4 address as its /24 network ("a.b.c", "" when it is not one).

    /24 is the coarsest test that is still useful: a host and the router that
    serves it differ only in the last octet, while the address a bridged
    container sees itself on (Docker's 172.18.0.0/16) never shares three octets
    with the router's LAN. Comparing all four octets would call every other host
    a different network; comparing fewer would call 172.16.x and 172.17.x one."""
    try:
        octets = [int(part) for part in str(addr or "").split(".")]
    except ValueError:
        return ""
    if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        return ""
    return ".".join(str(o) for o in octets[:3])


def _behind_another_nat(igd, address):
    """Whether *address* is on a different /24 than the IGD's own address.

    True means this process is behind another NAT, and Docker's bridge is the
    everyday case: the router that answered is on 192.168.40.0/24 while this
    process would name 172.18.0.3 as its internal client. The router cannot dial
    that address, so nothing this process sends naming it can be true. ONE helper
    for both the add path and the read-back path, so the two can never disagree
    about whether the bridge is there."""
    router, host = _network24(igd.get("from", "")), _network24(address)
    return bool(router and host and router != host)


def _tcp_answers(address, port, timeout=1.0):
    """Whether something accepts a TCP connection at *address*:*port*.

    The one fact available about an address this process cannot claim: a port the
    gateway forwards is only forwarded somewhere if the far end accepts a
    connection. A short timeout, because a user waits on this answer and an
    address that is not there refuses faster than it accepts."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((str(address or ""), int(port)))
        return True
    except (OSError, ValueError):
        return False
    finally:
        sock.close()


def _windows_default_gateway():
    """The default route's next hop from the IP helper API ("" when unreadable).

    ctypes, not a dependency: MIB_IPFORWARDROW is a fixed run of DWORDs and
    `GetIpForwardTable` fills it with the whole routing table."""
    import ctypes
    from ctypes import wintypes

    class MIB_IPFORWARDROW(ctypes.Structure):
        _fields_ = [
            ("dwForwardDest", wintypes.DWORD),
            ("dwForwardMask", wintypes.DWORD),
            ("dwForwardPolicy", wintypes.DWORD),
            ("dwForwardNextHop", wintypes.DWORD),
            ("dwForwardIfIndex", wintypes.DWORD),
            ("dwForwardType", wintypes.DWORD),
            ("dwForwardProto", wintypes.DWORD),
            ("dwForwardAge", wintypes.DWORD),
            ("dwForwardNextHopAS", wintypes.DWORD),
            ("dwForwardMetric1", wintypes.DWORD),
            ("dwForwardMetric2", wintypes.DWORD),
            ("dwForwardMetric3", wintypes.DWORD),
            ("dwForwardMetric4", wintypes.DWORD),
            ("dwForwardMetric5", wintypes.DWORD),
        ]

    iphlpapi = ctypes.WinDLL("iphlpapi.dll")
    size = wintypes.DWORD(0)
    iphlpapi.GetIpForwardTable(None, ctypes.byref(size), False)
    if not size.value:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetIpForwardTable(ctypes.cast(buf, ctypes.POINTER(MIB_IPFORWARDROW)),
                                  ctypes.byref(size), False) != 0:
        return ""
    rows = ctypes.cast(buf, ctypes.POINTER(MIB_IPFORWARDROW))
    count = size.value // ctypes.sizeof(MIB_IPFORWARDROW)
    best, best_metric = "", None
    for i in range(count):
        row = rows[i]
        # A 0.0.0.0/0 route is the default route (type 3 = on-link, 4 = via a
        # gateway); the lowest metric wins when several interfaces carry one.
        if row.dwForwardDest != 0 or row.dwForwardMask != 0:
            continue
        if row.dwForwardType not in (3, 4) or not row.dwForwardNextHop:
            continue
        metric = (row.dwForwardMetric1 or 0) + (row.dwForwardMetric4 or 0)
        if best_metric is None or metric < best_metric:
            best_metric = metric
            best = socket.inet_ntoa(struct.pack("<I", row.dwForwardNextHop))
    return best


def _route_print_default_gateway():
    """The default route from `route print 0.0.0.0` ("" when it is not there).

    The IP helper call above is the real answer; this is the fallback for the
    sessions where it is unavailable. The route ROW is numeric ("0.0.0.0 0.0.0.0
    192.168.1.1 192.168.1.5 25"), so only the localized header is skipped — by
    shape, never by name."""
    try:
        out = subprocess.run(["route", "print", "0.0.0.0"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            if parts[2] != "0.0.0.0":
                return parts[2]
    return ""


def default_gateway():
    """The default gateway's address, per platform ("" when it cannot be read).

    Everything here reads the OS's OWN routing state — the stdlib has no
    portable call for it, and guessing a common address (192.168.0.1) would send
    NAT-PMP requests to a host that may not be the gateway at all."""
    if sys.platform == "win32":
        return _windows_default_gateway() or _route_print_default_gateway()
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["route", "-n", "get", "default"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        for line in out.splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip() == "gateway":
                return value.strip().split("%")[0]
        return ""
    # Linux and the other unixes that keep the real table in /proc: the gateway
    # field there is little-endian hex.
    try:
        with open("/proc/net/route", encoding="ascii", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4 or parts[1] != "00000000":
                    continue
                if not int(parts[3], 16) & 0x2 or parts[2] == "00000000":
                    continue  # any other route, or one not through a gateway
                return socket.inet_ntoa(struct.pack("<I", int(parts[2], 16)))
    except (OSError, ValueError):
        return ""
    return ""


# --------------------------------------------------------------------------- #
# UPnP IGD
# --------------------------------------------------------------------------- #
def _localname(tag):
    """An XML tag's name without its namespace."""
    return str(tag).rsplit("}", 1)[-1]


def _parse_ssdp(data, sender):
    """One SSDP reply -> {location, st, server, from} ({} when it is not one)."""
    text = data.decode("utf-8", "replace")
    if not text.upper().startswith("HTTP/1."):
        return {}
    headers = {}
    for line in text.splitlines()[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return {"location": headers.get("location", ""), "st": headers.get("st", ""),
            "server": headers.get("server", ""), "from": sender[0]}


def ssdp_search(target, timeout=2.0, addr=SSDP_ADDR, port=SSDP_PORT, mx=1):
    """One M-SEARCH for `target` -> its replies: [{location, st, server, from}].

    `addr`/`port` are the discovery coordinates: the SSDP multicast group in
    production, and wherever a caller points them for a gateway that answers on
    its own port (or a test double)."""
    query = ("M-SEARCH * HTTP/1.1\r\n"
             f"HOST: {addr}:{port}\r\n"
             'MAN: "ssdp:discover"\r\n'
             f"MX: {int(mx)}\r\n"
             f"ST: {target}\r\n"
             "\r\n").encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    replies, seen = [], set()
    try:
        # TTL 2: an M-SEARCH must reach the gateway on THIS LAN and nothing
        # beyond it — the default TTL of 1 is why some IGDs never answer.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        sock.sendto(query, (addr, port))
        deadline = time.monotonic() + timeout
        while True:
            try:
                data, sender = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            reply = _parse_ssdp(data, sender)
            if reply and reply["location"] and reply["location"] not in seen:
                seen.add(reply["location"])
                replies.append(reply)
            if time.monotonic() >= deadline:
                break
    except OSError as e:
        raise GatewayUnreachable(f"the SSDP search could not be sent ({e})")
    finally:
        sock.close()
    return replies


def fetch_description(location, timeout=3.0):
    """An IGD's device description -> (service_type, control_url, device name).

    Raises GatewayUnreachable with the reason when the description cannot be
    read, is not XML, or lists no WAN connection service."""
    try:
        with urllib.request.urlopen(location, timeout=timeout) as r:
            body = r.read(_MAX_DESCRIPTION_BYTES)
    except Exception as e:
        raise GatewayUnreachable(f"{location} could not be read ({_joined([e])})") from e
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise GatewayUnreachable(f"{location} is not a device description ({e})") from e
    device, services = "", []
    for el in root.iter():
        name = _localname(el.tag)
        if name == "friendlyName" and not device:
            device = (el.text or "").strip()
        elif name == "service":
            stype = control = ""
            for child in el:
                child_name = _localname(child.tag)
                if child_name == "serviceType":
                    stype = (child.text or "").strip()
                elif child_name == "controlURL":
                    control = (child.text or "").strip()
            if stype and control:
                services.append((stype, control))
    for want in WAN_SERVICES:
        for stype, control in services:
            if want in stype:
                return stype, urljoin(location, control), device
    raise GatewayUnreachable(f"{location} describes no WAN port-mapping service "
                             f"({len(services)} service(s) listed)")


def _gateway_targets(gateways):
    """The named gateway addresses to search, cleaned: stripped, blanks dropped,
    repeats collapsed, the caller's order kept.

    A caller builds this list from config plus what it read off the routing table,
    so the same address twice — or a blank left by a setting that is off — must
    not become two searches: each one costs a wait with the user watching."""
    seen, out = set(), []
    for gw in gateways or ():
        text = str(gw or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _no_answer_text(ssdp_addr, ssdp_port, gateways):
    """What to tell the reader when nothing answered: every address searched.

    The multicast address alone hides the interesting half of the answer — a
    router that was NAMED and stayed silent — which is exactly what the panel
    reported for a router that answers a unicast M-SEARCH perfectly well."""
    named = _gateway_targets(gateways)
    text = f"no device answered the UPnP search on {ssdp_addr}:{ssdp_port}"
    if named:
        text += (", nor the M-SEARCH sent straight to "
                 + ", ".join(f"{gw}:{ssdp_port}" for gw in named))
    return text


def discover_igd(timeout=2.0, ssdp_addr=SSDP_ADDR, ssdp_port=SSDP_PORT,
                 gateways=()):
    """Find the IGD's WAN control endpoint -> (igd, errors).

    `igd` is None when nothing usable answered, and `errors` holds what each
    device that DID answer failed on, in the order it was tried — so an address
    in `gateways` whose answers could not be used is reported against that
    address. An address that answered NOTHING is not an error: silence is what
    the `no_gateway` state is for.

    The addresses in `gateways` are searched FIRST, by unicast on the same port,
    and the multicast group after them. A named address is an instruction, not a
    guess: it is what a native install read off its own routing table and what a
    container's owner typed in, and on a router that drops the multicast group
    (measured: an OpenWRT IGD that answers an M-SEARCH sent straight at its own
    LAN address and nothing on 239.255.255.250) the multicast pass can only
    burn its whole timeout before the search that works even starts — seven
    seconds per check on the install this was measured on, with a mapping
    watcher asking every twenty. The multicast group still follows, for whatever
    IGD is on this LAN with nobody being told an address, and it is the only
    search when no gateway is named."""
    errors, seen = [], set()
    # Named gateways first (each wait capped at a second: a router that stays
    # silent must not stack its silence on top of the multicast pass), then the
    # multicast coordinates.
    searches = [(gw, ssdp_port, min(timeout, 1.0), f"{gw}: ")
                for gw in _gateway_targets(gateways)]
    searches.append((ssdp_addr, ssdp_port, timeout, ""))
    for addr, port, wait, prefix in searches:
        for target in SEARCH_TARGETS:
            try:
                replies = ssdp_search(target, timeout=wait, addr=addr, port=port)
            except GatewayUnreachable as e:
                errors.append(f"{prefix}{e}")
                continue
            for reply in replies:
                location = reply["location"]
                if location in seen:
                    continue
                seen.add(location)
                try:
                    service, control, device = fetch_description(location)
                except GatewayUnreachable as e:
                    errors.append(f"{prefix}{e}")
                    continue
                return {"service": service, "control_url": control,
                        "location": location, "device": device,
                        "from": reply["from"]}, errors
    return None, errors


# The SOAP action's arguments, in the order the IGD schema declares them.
_SOAP_ACTIONS = {
    "AddPortMapping": ("NewRemoteHost", "NewExternalPort", "NewProtocol",
                       "NewInternalPort", "NewInternalClient", "NewEnabled",
                       "NewPortMappingDescription", "NewLeaseDuration"),
    "DeletePortMapping": ("NewRemoteHost", "NewExternalPort", "NewProtocol"),
    "GetExternalIPAddress": (),
    "GetSpecificPortMappingEntry": ("NewRemoteHost", "NewExternalPort",
                                    "NewProtocol"),
}


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _envelope(action, args, service):
    """The SOAP envelope for one action (absent args are sent as empty elements
    — an IGD requires the argument elements to be present)."""
    body = "".join(f"<{name}>{_xml_escape(args.get(name, ''))}</{name}>"
                   for name in _SOAP_ACTIONS[action])
    return ('<?xml version="1.0"?>\n'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action} xmlns:u="{service}">{body}</u:{action}>'
            "</s:Body></s:Envelope>")


def _fault_text(text):
    """A SOAP fault body as "description (UPnP error N)" ("" when not a fault)."""
    if not text or "<" not in text:
        return ""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ""
    desc = code = ""
    for el in root.iter():
        name = _localname(el.tag)
        if name == "errorDescription":
            desc = (el.text or "").strip()
        elif name == "faultstring" and not desc:
            desc = (el.text or "").strip()
        elif name == "errorCode":
            code = (el.text or "").strip()
    if desc or code:
        return (desc or "the gateway refused the request") + \
            (f" (UPnP error {code})" if code else "")
    return ""


def _response_args(text):
    """{argument: value} of a SOAP action response ({} when there is none)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    for el in root.iter():
        if not _localname(el.tag).endswith("Response"):
            continue
        return {_localname(child.tag): (child.text or "").strip() for child in el}
    return {}


def soap(igd, action, args=None, timeout=5.0):
    """One SOAP action on the IGD's control URL -> its response arguments.

    Raises GatewayRefused with the gateway's own fault text, or
    GatewayUnreachable when the control URL cannot be posted to."""
    service = igd["service"]
    req = urllib.request.Request(
        igd["control_url"], data=_envelope(action, args or {}, service).encode("utf-8"),
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPAction": f'"{service}#{action}"',
                 "Connection": "close"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(_MAX_SOAP_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        fault = _fault_text(raw.decode("utf-8", "replace"))
        raise GatewayRefused(fault or f"{action}: the gateway answered HTTP "
                                     f"{e.code} with no explanation") from e
    except Exception as e:
        raise GatewayUnreachable(f"{action}: {_joined([e])}") from e
    fault = _fault_text(text)
    if fault:
        raise GatewayRefused(fault)
    return _response_args(text)


def _upnp_external_ip(igd, timeout=3.0):
    """What the IGD states as its external address ("" when it will not say)."""
    try:
        args = soap(igd, "GetExternalIPAddress", timeout=timeout)
    except (GatewayRefused, GatewayUnreachable):
        return ""
    return args.get("NewExternalIPAddress", "").strip()


def _entry_verdict(args, port, ip, bridged=False):
    """One listed port-mapping entry as a verdict -> (state, verified, detail).

    The entry is judged against the request it answers: a mapping to another
    host, to another internal port or a disabled one is a real refusal, and only
    an entry that matches is ever reported as `mapped`. Shared by the add path
    below and by `read_port`, so both call the same listing `mapped`.

    `bridged` says this process is behind another NAT (see `_behind_another_nat`):
    then an internal client it never named is NOT "another device holds it". A
    container behind Docker's bridge cannot state which address on the router's
    network is its own, and the entry the router really holds points at the HOST
    — whose address may well be the one at the end of a working forward. So the
    entry is accepted as a mapping and judged by the one fact available: whether
    that address answers on the port."""
    client = args.get("NewInternalClient", "").strip()
    internal = args.get("NewInternalPort", "").strip()
    enabled = args.get("NewEnabled", "").strip()
    if enabled not in ("", "1"):
        return "refused", False, (f"the gateway lists the mapping as disabled "
                                  f"(NewEnabled={enabled})")
    if internal and internal != str(port):
        return "refused", False, (f"external port {port} is mapped to internal "
                                  f"port {internal} instead")
    if bridged and client:
        where = f"{client}:{internal or port}"
        if _tcp_answers(client, internal or port):
            return "mapped", True, (f"the gateway lists external port {port} -> "
                                    f"{where}, and {client} accepts a connection "
                                    f"there; this process is behind Docker's "
                                    f"bridge, so it cannot confirm that address "
                                    f"is its own")
        return "refused", False, (f"the gateway forwards external port {port} to "
                                  f"{where}, and nothing there accepts a "
                                  f"connection — the forward leads nowhere")
    if client and ip and client != ip:
        return "refused", False, (f"external port {port} is mapped to {client}, "
                                  f"not to this machine ({ip}) — another device "
                                  f"holds it")
    return "mapped", True, (f"the gateway lists external port {port} -> "
                            f"{client or ip}:{internal or port}")


def _upnp_verify(igd, port, ip, timeout=3.0, bridged=False):
    """Was the mapping really made? -> (state, verified, detail).

    `GetSpecificPortMappingEntry` is the only way to ask the router what it
    actually holds, and not every IGD implements it. An unsupported-action fault
    means "cannot verify" — the accepted add still stands — while an entry
    pointing at another host, a disabled entry or a 714 "no such entry in array"
    is a real refusal. Reporting either of those as a success is exactly the lie
    this module exists to prevent. `bridged` goes straight to the verdict: behind
    another NAT an entry pointing elsewhere is judged by what answers there, not
    by the address this process cannot state."""
    try:
        args = soap(igd, "GetSpecificPortMappingEntry",
                    {"NewRemoteHost": "", "NewExternalPort": str(port),
                     "NewProtocol": "TCP"}, timeout=timeout)
    except GatewayRefused as e:
        text = str(e)
        if "714" in text or "NoSuchEntry" in text:
            return "refused", False, (f"the gateway does not list the mapping it "
                                      f"just accepted: {text}")
        return "mapped", False, ("the gateway accepted the mapping; it does not "
                                 f"support reading it back ({text})")
    except GatewayUnreachable as e:
        return "mapped", False, (f"the gateway accepted the mapping; the check "
                                 f"could not be made ({e})")
    return _entry_verdict(args, port, ip, bridged=bridged)


def upnp_open(port, *, ip="", gateway="", description=DEFAULT_DESCRIPTION,
              timeout=3.0, ssdp_addr=SSDP_ADDR, ssdp_port=SSDP_PORT):
    """Add a UPnP IGD mapping for TCP *port* -> the structured result.

    `gateway` is the router's LAN address when the caller knows it (the config's
    `soulseek_router_ip`): the multicast search is made first, and a router that
    drops the multicast group is then asked by UNICAST at that address before
    anything is reported as unanswered. `ip` is the internal client to name; when
    it is empty this process works it out and refuses to name an address the
    router cannot dial (see the bridge guard below)."""
    port = int(port)
    try:
        igd, errors = discover_igd(timeout=timeout, ssdp_addr=ssdp_addr,
                                   ssdp_port=ssdp_port, gateways=(gateway,))
    except GatewayUnreachable as e:
        return _out("no_gateway", method="upnp", port=port,
                    attempts=[_attempt("upnp", False, False, str(e))],
                    detail=f"UPnP: {e}")
    if igd is None:
        why = _joined(errors) or _no_answer_text(ssdp_addr, ssdp_port, (gateway,))
        return _out("unsupported" if errors else "no_gateway", method="upnp",
                    port=port,
                    attempts=[_attempt("upnp", bool(errors), False, why)],
                    detail=f"UPnP: {why}")
    internal = ip or local_ip(igd.get("from") or gateway)
    device = igd.get("device") or igd["location"]
    if not internal:
        return _out("error", method="upnp", port=port, gateway=igd.get("from", ""),
                    attempts=[_attempt("upnp", True, False,
                                       f"{device} answered, but this machine's "
                                       f"address on its network could not be read")],
                    detail="this machine's address could not be determined, so "
                           "there is nothing to forward the port to")
    # The hazard a bridged container lives in: a UPnP mapping names the internal
    # client, and this process would name an address on ITS side of the bridge
    # (Docker's 172.18.0.x) while the router is on its own LAN. The router cannot
    # dial that address, so the mapping would be a forward to nowhere that still
    # reads as success. An `ip` the CALLER named is trusted — it may know a route
    # this process cannot see — but an address worked out here is not sent.
    bridged = not ip and _behind_another_nat(igd, internal)
    if bridged:
        return _out("unsupported", method="upnp", port=port, ip=internal,
                    gateway=igd.get("from", ""),
                    attempts=[_attempt("upnp", True, False,
                                       f"{device}: no AddPortMapping was sent — "
                                       f"{internal} is not on the router's network "
                                       f"({igd.get('from', '')})")],
                    detail=(f"{device} answered, but a UPnP mapping has to name "
                            f"the internal client, and this process would name "
                            f"{internal} — an address on its own side of a bridge, "
                            f"not on {igd.get('from', '')}'s network, so the "
                            f"router cannot dial it. AddPortMapping was not sent. "
                            f"NAT-PMP is the method that applies here: it carries "
                            f"no internal address at all (the gateway maps the "
                            f"port to whoever asked), so it survives the bridge"))
    external = _upnp_external_ip(igd, timeout=timeout)
    try:
        soap(igd, "AddPortMapping",
             {"NewRemoteHost": "", "NewExternalPort": str(port),
              "NewProtocol": "TCP", "NewInternalPort": str(port),
              "NewInternalClient": internal, "NewEnabled": "1",
              # 0 = no lease requested: the mapping stays until it is removed.
              "NewPortMappingDescription": description,
              "NewLeaseDuration": "0"},
             timeout=max(timeout, 5.0))
    except GatewayRefused as e:
        return _out("refused", method="upnp", port=port, ip=internal,
                    external=external, gateway=igd.get("from", ""),
                    attempts=[_attempt("upnp", True, False, f"{device}: {e}")],
                    detail=f"{device} refused to forward port {port}: {e}")
    except GatewayUnreachable as e:
        return _out("error", method="upnp", port=port, ip=internal,
                    external=external, gateway=igd.get("from", ""),
                    attempts=[_attempt("upnp", True, False, f"{device}: {e}")],
                    detail=f"{device} stopped answering: {e}")
    state, verified, detail = _upnp_verify(igd, port, internal, timeout=timeout,
                                           bridged=bridged)
    return _out(state, ok=state == "mapped", method="upnp", port=port, ip=internal,
                external=external, gateway=igd.get("from", ""), verified=verified,
                attempts=[_attempt("upnp", True, state == "mapped",
                                   f"{device}: {detail}")],
                detail=f"{device}: {detail}")


def upnp_close(port, *, gateway="", timeout=3.0, ssdp_addr=SSDP_ADDR,
               ssdp_port=SSDP_PORT):
    """Remove the UPnP mapping of TCP *port* -> the structured result.

    `gateway` is searched by unicast when the multicast group goes unanswered,
    exactly as in `upnp_open`: a router this process could not discover is
    usually one this process cannot clean up after either."""
    port = int(port)
    try:
        igd, errors = discover_igd(timeout=timeout, ssdp_addr=ssdp_addr,
                                   ssdp_port=ssdp_port, gateways=(gateway,))
    except GatewayUnreachable as e:
        return _out("no_gateway", method="upnp", port=port,
                    attempts=[_attempt("upnp", False, False, str(e))],
                    detail=f"UPnP: {e}")
    if igd is None:
        why = _joined(errors) or _no_answer_text(ssdp_addr, ssdp_port, (gateway,))
        return _out("unsupported" if errors else "no_gateway", method="upnp",
                    port=port,
                    attempts=[_attempt("upnp", bool(errors), False, why)],
                    detail=f"UPnP: {why}")
    device = igd.get("device") or igd["location"]
    try:
        soap(igd, "DeletePortMapping",
             {"NewRemoteHost": "", "NewExternalPort": str(port),
              "NewProtocol": "TCP"}, timeout=max(timeout, 5.0))
    except GatewayRefused as e:
        text = str(e)
        # 714 = no such entry: the mapping is already gone, which is what the
        # caller asked for. Everything else is the gateway's own refusal.
        if "714" in text or "NoSuchEntry" in text:
            return _out("released", ok=True, method="upnp", port=port,
                        gateway=igd.get("from", ""),
                        attempts=[_attempt("upnp", True, True, f"{device}: {text}")],
                        detail=f"{device}: no mapping of port {port} was there to "
                               f"remove ({text})")
        return _out("refused", method="upnp", port=port, gateway=igd.get("from", ""),
                    attempts=[_attempt("upnp", True, False, f"{device}: {e}")],
                    detail=f"{device} refused to remove the mapping of port "
                           f"{port}: {e}")
    except GatewayUnreachable as e:
        return _out("error", method="upnp", port=port, gateway=igd.get("from", ""),
                    attempts=[_attempt("upnp", True, False, f"{device}: {e}")],
                    detail=f"{device} stopped answering: {e}")
    return _out("released", ok=True, method="upnp", port=port,
                gateway=igd.get("from", ""), verified=True,
                attempts=[_attempt("upnp", True, True,
                                   f"{device} removed the mapping of port {port}")],
                detail=f"{device}: the mapping of port {port} was removed")


# --------------------------------------------------------------------------- #
# NAT-PMP (RFC 6886)
# --------------------------------------------------------------------------- #
def _pmp_exchange(gateway, payload, opcode, timeout, attempts, port):
    """Send one NAT-PMP request and read its answer -> ((result, epoch, raw), data).

    Retransmits with the doubling interval the RFC prescribes: a lost UDP answer
    must not be read as "this gateway has no NAT-PMP"."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    delay = timeout
    try:
        sock.settimeout(delay)
        try:
            sock.connect((gateway, port))
        except OSError as e:
            raise GatewayUnreachable(f"{gateway} is not reachable ({e})") from e
        for _ in range(max(1, attempts)):
            try:
                sock.send(payload)
            except OSError as e:
                raise GatewayUnreachable(f"the request could not be sent ({e})") from e
            try:
                data, _peer = sock.recvfrom(64)
            except socket.timeout:
                delay = min(delay * 2, 8.0)
                sock.settimeout(delay)
                continue
            except OSError as e:
                # ICMP port-unreachable: nothing listens on that port there.
                raise GatewayUnreachable(f"{gateway} answered nothing on NAT-PMP "
                                         f"({e})") from e
            # Version, and the response opcode (request + 128) — anything else is
            # somebody else's datagram, so it is not an answer.
            if len(data) < 8 or data[0] != PMP_VERSION or data[1] != opcode + 128:
                continue
            code, epoch = struct.unpack("!HI", data[2:8])
            return (code, epoch, data), data
    finally:
        sock.close()
    raise GatewayUnreachable(f"{gateway} did not answer after {attempts} "
                             f"request(s) on port {port}")


def pmp_external_address(gateway, timeout=1.0, attempts=3, port=NATPMP_PORT):
    """The gateway's own external address via NAT-PMP ("" when it will not say)."""
    try:
        (result, _epoch, data), _raw = _pmp_exchange(
            gateway, struct.pack("!BB", PMP_VERSION, 0), 0, timeout, attempts, port)
    except (GatewayRefused, GatewayUnreachable):
        return ""
    if result or len(data) < 12:
        return ""
    return socket.inet_ntoa(data[8:12])


def natpmp_open(port, *, gateway="", lifetime=PMP_LIFETIME, timeout=1.0,
                attempts=3, pmp_port=NATPMP_PORT):
    """Map TCP *port* on the gateway via NAT-PMP -> the structured result."""
    port = int(port)
    gw = gateway or default_gateway()
    if not gw:
        return _out("no_gateway", method="natpmp", port=port,
                    attempts=[_attempt("natpmp", False, False,
                                       "no default gateway could be read from this "
                                       "system's routing table")],
                    detail="no default gateway could be read, so no NAT-PMP "
                           "request was sent")
    external = pmp_external_address(gw, timeout=timeout, attempts=attempts,
                                    port=pmp_port)
    # NAT-PMP maps "the host that asked", so the request itself names no address
    # — but this machine still knows which interface it left by, and that is
    # what a reader needs (and what the UPnP path states as the internal client).
    internal = local_ip(gw)
    # Opcode 2 = TCP (Soulseek peers connect to the listener); internal and
    # requested external port are the same, and the answer states the lifetime
    # the gateway actually granted.
    payload = struct.pack("!BBHHHI", PMP_VERSION, 2, 0, port, port, int(lifetime))
    try:
        (result_code, _epoch, data), _raw = _pmp_exchange(
            gw, payload, 2, timeout, attempts, pmp_port)
    except GatewayRefused as e:
        return _out("refused", method="natpmp", port=port, ip=internal,
                    external=external, gateway=gw,
                    attempts=[_attempt("natpmp", True, False, str(e))],
                    detail=f"the gateway at {gw} refused the mapping: {e}")
    except GatewayUnreachable as e:
        return _out("no_gateway", method="natpmp", port=port, ip=internal,
                    external=external,
                    attempts=[_attempt("natpmp", False, False, str(e))],
                    detail=f"NAT-PMP: {e}")
    if result_code:
        text = _pmp_result_text(result_code)
        return _out("refused", method="natpmp", port=port, ip=internal,
                    external=external, gateway=gw,
                    attempts=[_attempt("natpmp", True, False,
                                       f"NAT-PMP {text} (result {result_code})")],
                    detail=f"the gateway at {gw} refused the mapping: {text}")
    if len(data) < 16:
        return _out("error", method="natpmp", port=port, ip=internal,
                    external=external, gateway=gw,
                    attempts=[_attempt("natpmp", True, False,
                                       "the answer was too short to be a map "
                                       "response")],
                    detail="the gateway answered with a malformed NAT-PMP mapping "
                           "response")
    mapped_internal, mapped, granted = struct.unpack("!HHI", data[8:16])
    if not mapped:
        return _out("refused", method="natpmp", port=port, ip=internal,
                    external=external, gateway=gw,
                    attempts=[_attempt("natpmp", True, False,
                                       "the gateway reported no external port")],
                    detail="the gateway reported success but mapped no external port")
    return _out("mapped", ok=True, method="natpmp", port=port, ip=internal,
                external=external, gateway=gw, verified=True,
                expires_at=(time.time() + granted) if granted else 0.0,
                attempts=[_attempt("natpmp", True, True,
                                   f"the gateway mapped external port {mapped} -> "
                                   f"this machine:{mapped_internal} for {granted}s")],
                detail=f"NAT-PMP {gw}: external port {mapped} is forwarded here "
                       f"for {granted}s")


def _pmp_result_text(result_code):
    return PMP_RESULTS.get(result_code, f"result code {result_code}")


def natpmp_close(port, *, gateway="", timeout=1.0, attempts=3,
                 pmp_port=NATPMP_PORT):
    """Remove the NAT-PMP mapping of TCP *port* -> the structured result.

    RFC 6886 removes a mapping with the SAME request carrying a zero lifetime,
    so a successful answer means the mapping is gone — whether or not one was
    there to begin with."""
    port = int(port)
    gw = gateway or default_gateway()
    if not gw:
        return _out("no_gateway", method="natpmp", port=port,
                    attempts=[_attempt("natpmp", False, False,
                                       "no default gateway could be read")],
                    detail="no default gateway could be read, so no NAT-PMP "
                           "request was sent")
    payload = struct.pack("!BBHHHI", PMP_VERSION, 2, 0, port, port, 0)
    try:
        (result_code, _epoch, _data), _raw = _pmp_exchange(
            gw, payload, 2, timeout, attempts, pmp_port)
    except GatewayRefused as e:
        return _out("refused", method="natpmp", port=port, gateway=gw,
                    attempts=[_attempt("natpmp", True, False, str(e))],
                    detail=f"the gateway at {gw} refused to remove the mapping: {e}")
    except GatewayUnreachable as e:
        return _out("no_gateway", method="natpmp", port=port,
                    attempts=[_attempt("natpmp", False, False, str(e))],
                    detail=f"NAT-PMP: {e}")
    if result_code:
        text = _pmp_result_text(result_code)
        return _out("refused", method="natpmp", port=port, gateway=gw,
                    attempts=[_attempt("natpmp", True, False,
                                       f"NAT-PMP {text} (result {result_code})")],
                    detail=f"the gateway at {gw} refused to remove the mapping: {text}")
    return _out("released", ok=True, method="natpmp", port=port, gateway=gw,
                verified=True,
                attempts=[_attempt("natpmp", True, True,
                                   f"the gateway dropped the mapping of port {port}")],
                detail=f"NAT-PMP {gw}: the mapping of port {port} was dropped")


# --------------------------------------------------------------------------- #
# One entry point
# --------------------------------------------------------------------------- #
def read_port(port, *, ip="", gateway="", timeout=1.5, ssdp_addr=SSDP_ADDR,
              ssdp_port=SSDP_PORT, pmp_port=NATPMP_PORT):
    """What a gateway holds for TCP *port* right now -> the structured result.

    One discovery answers both halves of the question a user asks of a port
    ("is it forwarded, and can anything reach it"): whether the gateway currently
    LISTS an entry for *port*, and what it states as its WAN address. UPnP's
    `GetSpecificPortMappingEntry` is the only request that reads an entry back —
    NAT-PMP has none (RFC 6886 §3.4 maps a port or drops it, and says nothing
    about what is already there), so a mapping a NAT-PMP gateway granted is only
    ever known from the answer that made it.

    `state` is `mapped` only when the gateway lists the entry and it is this
    machine's (the same judgment `upnp_open` applies to the entry it asks for);
    `refused` carries the gateway's own words (UPnP 714 = no such entry in the
    array, which is a router that really has no such mapping); `unsupported`
    means a gateway answered but cannot read an entry back; `no_gateway` means
    nothing answered on either method. `external_ip` is set only when a gateway
    stated one, and `expires_at` only when it stated a lease (0 = it stated
    none, so the entry does not expire).

    A read never changes anything: nothing here adds or removes a mapping.

    Behind another NAT (Docker's bridge) this process cannot state which address
    on the router's network is its own, so an entry that points at a different
    address is not read as another device holding the port: it is judged by
    whether that address answers on the port, which is what makes the entry
    either a forward the world can use or a refusal. `gateway`, when the caller
    names it, is also where the unicast half of discovery goes."""
    port = int(port)
    try:
        igd, errors = discover_igd(timeout=timeout, ssdp_addr=ssdp_addr,
                                   ssdp_port=ssdp_port, gateways=(gateway,))
    except GatewayUnreachable as e:
        igd, errors = None, [str(e)]
    if igd is None:
        gw = gateway or default_gateway()
        wan = pmp_external_address(gw, timeout=min(timeout, 1.0),
                                   port=pmp_port) if gw else ""
        why = _joined(errors) or _no_answer_text(ssdp_addr, ssdp_port, (gateway,))
        # A gateway that answers NAT-PMP can state its WAN address but still
        # cannot be ASKED what it holds, so this is "cannot read back", never
        # "no mapping" — reporting the second would tell the user their forward
        # is missing when nothing here could have seen it.
        return _out("unsupported" if (errors or wan) else "no_gateway",
                    method="natpmp" if wan else "upnp", port=port,
                    external=wan, gateway=gw if wan else "",
                    attempts=[_attempt("upnp", bool(errors), False, f"UPnP: {why}"),
                              _attempt("natpmp", bool(wan), False,
                                       f"NAT-PMP stated the WAN address {wan}" if wan
                                       else "NAT-PMP stated no WAN address")],
                    detail=(f"UPnP: {why}. NAT-PMP cannot be asked what it holds "
                            f"— it has no request that reads a mapping back — so a "
                            f"mapping it granted can only be seen in the answer "
                            f"that made it."))
    internal = ip or local_ip(igd.get("from") or gateway)
    # The address this process would state as its own is what the verdict is
    # judged against, so the bridge fact is computed from THAT address rather
    # than from whether a caller passed one in: a caller passing on what
    # `local_ip` would say is the same address, and reading an entry from behind
    # the bridge must not turn into "another device holds it" (see
    # `_entry_verdict`).
    bridged = _behind_another_nat(igd, internal)
    device = igd.get("device") or igd["location"]
    external = _upnp_external_ip(igd, timeout=timeout)
    attempts = ([_attempt("upnp", True, True,
                          f"{device} stated its WAN address as {external}")]
                if external else [])
    try:
        args = soap(igd, "GetSpecificPortMappingEntry",
                    {"NewRemoteHost": "", "NewExternalPort": str(port),
                     "NewProtocol": "TCP"}, timeout=max(timeout, 3.0))
    except GatewayRefused as e:
        text = str(e)
        if "714" in text or "NoSuchEntry" in text:
            return _out("refused", method="upnp", port=port, ip=internal,
                        external=external, gateway=igd.get("from", ""),
                        attempts=attempts + [_attempt("upnp", True, False,
                                                      f"{device}: {text}")],
                        detail=f"{device} lists no mapping of port {port} ({text})")
        return _out("unsupported", method="upnp", port=port, ip=internal,
                    external=external, gateway=igd.get("from", ""),
                    attempts=attempts + [_attempt("upnp", True, False,
                                                  f"{device}: {text}")],
                    detail=f"{device} cannot read a mapping back ({text})")
    except GatewayUnreachable as e:
        return _out("no_gateway", method="upnp", port=port, ip=internal,
                    external=external, gateway=igd.get("from", ""),
                    attempts=attempts + [_attempt("upnp", True, False,
                                                  f"{device}: {e}")],
                    detail=f"{device} stopped answering: {e}")
    state, verified, detail = _entry_verdict(args, port, internal, bridged=bridged)
    # The entry states what is LEFT of its lease, as a duration — not when it
    # runs out — and 0 for an entry the gateway does not expire.
    try:
        lease = int(args.get("NewLeaseDuration") or 0)
    except ValueError:
        lease = 0
    if state == "mapped" and lease > 0:
        detail = f"{detail}, leased for another {lease}s"
    return _out(state, ok=state == "mapped", method="upnp", port=port,
                ip=args.get("NewInternalClient", "").strip() or internal,
                external=external, gateway=igd.get("from", ""), verified=verified,
                expires_at=(time.time() + lease) if state == "mapped" else 0.0,
                attempts=attempts + [_attempt("upnp", True, state == "mapped",
                                              f"{device}: {detail}")],
                detail=f"{device}: {detail}")


# The order a refusal is worth reporting in: the gateway's own words first, then
# what this side did wrong, then a device that cannot map at all, and only then
# the network where nothing answered.
_WORST_FIRST = ("refused", "error", "unsupported", "no_gateway")


def _adopt(result, results, attempts):
    """Attach everything learned along the way to the verdict that ended the
    search: a method that mapped the port must still report what the other one
    was told, or the UI cannot explain why the fallback was needed."""
    out = dict(result)
    out["attempts"] = attempts
    tried = [t for r in results for t in r["tried"]]
    out["tried"] = tried or out["tried"]
    # The address facts are about this NETWORK, not about the method that
    # happened to report them.
    for src in results:
        for key in ("external_ip", "internal_ip", "gateway"):
            out[key] = out.get(key) or src.get(key, "")
    return out


def _merge(results, attempts, port):
    """One verdict out of the per-method results, with everything attached."""
    if not results:
        return _out("omitted", port=port,
                    detail="no mapping method was enabled", attempts=attempts)
    picked = None
    for state in _WORST_FIRST:
        picked = next((r for r in results if r["state"] == state), None)
        if picked:
            break
    return _adopt(picked or results[-1], results, attempts)


def open_port(port, *, ip="", gateway="", description=DEFAULT_DESCRIPTION,
              lifetime=PMP_LIFETIME, timeout=3.0, ssdp_addr=SSDP_ADDR,
              ssdp_port=SSDP_PORT, pmp_port=NATPMP_PORT,
              methods=("upnp", "natpmp")):
    """Forward TCP *port* to this machine: UPnP IGD first, then NAT-PMP.

    Returns ONE structured result: `state`/`ok` say what really happened,
    `tried` says what each method was told, `external_ip` is set only when a
    gateway stated one, and a refusal carries the gateway's own words. NAT-PMP
    runs whenever UPnP did not produce a mapping — a gateway that cannot be
    discovered and one that answers and then refuses both leave the other method
    to try, and so does a bridged container, where UPnP is refused outright
    rather than forwarding the port to an address its router cannot dial.
    `gateway` is the router's LAN address when the caller knows it; it is where
    the unicast half of the UPnP search goes."""
    port = int(port)
    attempts, results = [], []
    if "upnp" in methods:
        upnp = upnp_open(port, ip=ip, gateway=gateway, description=description,
                         timeout=timeout, ssdp_addr=ssdp_addr, ssdp_port=ssdp_port)
        results.append(upnp)
        attempts += upnp["attempts"]
        if upnp["ok"]:
            return _adopt(upnp, results, attempts)
    if "natpmp" in methods:
        pmp = natpmp_open(port, gateway=gateway, lifetime=lifetime,
                          timeout=min(timeout, 2.0), pmp_port=pmp_port)
        results.append(pmp)
        attempts += pmp["attempts"]
        if pmp["ok"]:
            return _adopt(pmp, results, attempts)
    return _merge(results, attempts, port)


def close_port(port, *, gateway="", timeout=3.0, ssdp_addr=SSDP_ADDR,
               ssdp_port=SSDP_PORT, pmp_port=NATPMP_PORT,
               methods=("upnp", "natpmp")):
    """Remove the mapping of TCP *port*: UPnP `DeletePortMapping`, then NAT-PMP.

    `state` is `released` only when a gateway confirmed the removal, `refused`
    with its reason when one refused, and `no_gateway` when neither method got an
    answer — which is never reported as a released mapping."""
    port = int(port)
    attempts, results = [], []
    if "upnp" in methods:
        upnp = upnp_close(port, gateway=gateway, timeout=timeout,
                          ssdp_addr=ssdp_addr, ssdp_port=ssdp_port)
        results.append(upnp)
        attempts += upnp["attempts"]
        if upnp["ok"]:
            return _adopt(upnp, results, attempts)
    if "natpmp" in methods:
        pmp = natpmp_close(port, gateway=gateway, timeout=min(timeout, 2.0),
                           pmp_port=pmp_port)
        results.append(pmp)
        attempts += pmp["attempts"]
        if pmp["ok"]:
            return _adopt(pmp, results, attempts)
    return _merge(results, attempts, port)
