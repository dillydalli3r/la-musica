# la musica 4.1.9 — the address the network hands out, read back and shown

"Still aint working." Fair — 4.1.8 proved the *port* was reachable from outside
and explained why a client on your own LAN cannot use it, but nothing in the app
could say **which address the Soulseek server is handing peers**, so the one line
that decides the whole question was still invisible. It is visible now.

## The row: `Peers are told`

`Test port` gained a seventh row, and it is the only one that asks the NETWORK
rather than this machine. slskd can browse a user, and browsing *our own
username* makes it ask the Soulseek server where this account is and dial it —
both outcomes are the answer. A file list means the address the server publishes
is this one and the path answers from wherever slskd runs; a failure names the
address it dialled, which is exactly what peers are told. The address is parsed
out of slskd's own words, so a wording change upstream leaves the row saying it
could not read it — never naming a wrong one. Two failure shapes are told apart:
an address whose PORT is not the configured listen port is a `fail` with its
remedy ("restart slskd so it registers its listen port again"), and an address
that matches and refuses is `unknown`, naming the address and saying what a
client on the same network gets.

Run against the live install, the whole probe now reads:

```
verdict: ok | listen port: 50000 | container: True
  [ok     ] listen       a TCP connection to 127.0.0.1:50000 was accepted, and slskd holds the port
  [ok     ] publish      the host publishes TCP 50000 to this container
  [ok     ] mapping      OpenWRT router: the gateway lists external port 50000 -> 192.168.40.62:50000
  [ok     ] address      the mapping points at 192.168.40.62 — the HOST's address
  [unknown] self-connect the connection to 216.212.53.255:50000 was not accepted (timed out) …
  [unknown] peers_told   peers are told 216.212.53.255:50000, and this machine cannot dial it from where
                         slskd runs. A router without NAT hairpinning refuses its own public address to a
                         connection that starts inside — which is also why a Soulseek client on the SAME
                         network cannot browse this share …
  [ok     ] network      slskd reports it is signed in to the Soulseek network.
```

So the address peers are given is right, the router forwards it, the listener is
there and the daemon is signed in — and the only path that fails is the one that
starts inside the house.

## What that means for your client

SoulseekQt is handed `216.212.53.255:50000` (the row above, and slskd's own
failure message, both say so) and dials it from your own network, where the
router refuses its own public address. That is the router's **NAT loopback /
reflection** setting, not anything on this side: turn it on and the local client
works too. Otherwise the client has to be on another network — and note that
logging into SoulseekQt as `dillydallier` from elsewhere takes the account over
from slskd (one login per username) and re-registers *its* address with the
server, so a test on another network wants a different account, or a friend.

Verified: `tools/test_soulseek_port.py` 88 checks (81 at 4.1.8; the seven new
cases cover the answered browse, the hairpin shape, a network dialling the wrong
port, slskd's message without an address, a missing username and a stopped
daemon), the row read live against the owner's own install and network (output
above), and all ten version copies at 4.1.9.
