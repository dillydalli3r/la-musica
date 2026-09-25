# la musica 4.1.4 — one number per pair, and a mapping that reaches the router

Everything below was measured on the install that reported it, not reasoned
about; the commands and the answers are in the notes.

## The two recommendation shelves carry one number (issue #54)

An album page showed **9** "Recommended (Local)" covers beside **12**
"Recommended (Online)" suggestions, and a track page showed **20** local rows
beside those same 12. Same headings, same chrome, two lists of the same thing —
and the reader counts them, so the pair read as a fault in whichever shelf came
back shorter.

The cap was two different numbers in two different places: `server/recommend.py`
defaulted to 12 covers but **20** track rows (`DEFAULT_TRACK_LIMIT`, justified by
"a track shelf is read row by row"), while the web asked the online providers for
12. Now there is one number:

* `mlo`'s scorer has a single `DEFAULT_LIMIT = 12` for either target — albums or
  tracks — and the docstring says why (the shelves are read as a pair).
* `web/src/components/RecommendShelf.tsx` exports `SHELF_LIMIT = 12`: the one
  number every "Recommended" shelf asks for. `MoreLikeThis` sends it in the
  request instead of leaning on the server's default, `OnlineRecommendations`
  uses it, and the standalone Recommended page (which carried its own `LIMIT =
  20` under the same title) uses it too. A page may still pass its own `limit`.
* **Both shelves now print their count** — the local one in the same slot and
  the same shape as the online shelf's "12 suggestions". "9 local albums" beside
  "12 online suggestions" is then a true reading of a small library rather than a
  shelf that lost three rows.

Verified: `python tools/test_recommendations.py` (a new wide-library case — 30
records sharing one genre, mood, energy and era — proves both targets come back
with exactly the default and that an explicit `limit=20` still gets 20; it fails
against the pre-change module with `AssertionError: 20` on the track shelf).
`node tools/check_library_az.mjs` keeps the request body the page sent and
asserts it carries `limit: 12`, and reads the count off the shelf's heading line
— 52/52 checks pass.

## The port mapping can reach the router from inside the container

The Sharing card said `port unconfirmed` and, under it, "UPnP: no device
answered the UPnP search on 239.255.255.250:1900 — automatic opening cannot
reach the router from here (the gateway this process sees is Docker's bridge)".
Both halves of that sentence turned out to be true, and the router turned out to
be fine:

| measurement | answer |
|---|---|
| multicast `M-SEARCH` to `239.255.255.250:1900`, host *and* container, IGD + service targets, 6 s windows | **0 replies** — this router ignores multicast searches |
| unicast `M-SEARCH` to `192.168.40.1:1900` | answered: `LOCATION http://192.168.40.1:43077/rootDesc.xml`, "OpenWRT router" — works from inside the container too |
| its description | UPnP **IGD v2**: `InternetGatewayDevice:2`, service `WANIPConnection:2` → `/ctl/IPConn` |
| `GetExternalIPAddress` from inside the container | `216.212.53.255` |
| NAT-PMP external-address request (RFC 6886, read-only) | answered: `216.212.53.255`, result code 0 |
| `portmap.natpmp_open(50000, gateway="192.168.40.1")` — the app's own code | `state: mapped, ok: True, verified: True` — "external port 50000 is forwarded here for 7200s" |

So the app's mapping code was never the problem; what was missing was any way to
*reach* the router. Four changes:

* **The search takes gateway addresses.** `discover_igd(gateways=(…))` searches
  each name by unicast (multicast first, then the named addresses, first usable
  IGD wins), and the `gateway` argument that `upnp_open`, `upnp_close`,
  `read_port`, `open_port` and `close_port` already accepted is now actually
  searched instead of only being used to guess a local address. The IGD **v2**
  device target joined `SEARCH_TARGETS`.
* **A new setting names the router**: `soulseek_router_ip` (Settings → Soulseek →
  "Router IP for port mapping (blank = auto-detect)"). Blank keeps
  auto-detection, which is what a native install wants; a container fills it in
  because Docker's bridge is the only gateway it can see.
* **A UPnP mapping is refused when the app is behind another NAT.** Add an
  address comparison before `AddPortMapping`: when the address this process would
  name is not on the gateway's own network (172.18.0.3 against a router on
  192.168.40.0/24) the app will not tell the router to forward the port to an
  address it cannot dial — it says so and lets **NAT-PMP** do the work, which
  carries no internal address at all: the gateway maps the port to the
  requester's own source, which is exactly the host. Reading an entry back
  follows the same rule, so a mapping that names the HOST is judged by whether
  that address answers rather than dismissed as "another device".
* **The app asks before it tells.** `portmap_sync` now reads what the router
  already holds and only asks for a mapping when nothing does. This is not
  theoretical: a NAT-PMP map for the same external port *replaced* the standing
  forward with a two-hour lease while this release was being written, and
  dropping that lease closed a port that had been open all along.

The container note on the Sharing card now names the setting instead of ending
at "a router cannot forward to a container address".

Verified: the app's own code granted and released a mapping against the live
OpenWRT router from inside the container (above), and the router's port listing
was read back with `GetSpecificPortMappingEntry` before and after
(`50000 → 192.168.40.62:50000`, description "la musica Soulseek", lease the
router's 7-day maximum). Reachability was proved from outside the network, not
inferred: `check-host.net` TCP checks against `216.212.53.255:50000` answered
from Tel Aviv and Tokyo before the change and from Spain, Iran and Turkey after
it. The `mlo/portmap.py` suite gained cases for a multicast-silent router found
only when named, the v2 target, the behind-a-bridge refusal (no `AddPortMapping`
is sent) and the bridged read-back verdicts.
