# la musica 4.1.10 — the share answers its own browse, and the audit that proves the rest

4.1.9 read the address the network publishes and explained the wall. This release
is the two halves that finish the job: a full audit of "can a peer really read
this share", and the one browse that never could — your own — answered from the
share itself.

## The audit (issue #57), measured on the owner's install

- **From the internet, the share is fine.** A throwaway Soulseek account, sharing
  nothing, on a GitHub runner — running slskd v0.26.0, the same binary this app
  installs — browsed `dillydallier` three times: HTTP 200 in 0.09 s / 0.44 s /
  0.29 s, **15 directories and 165 files** every time, with real names (Talking
  Heads *Remain in Light* FLACs, Radiohead *Kid A* and *Amnesiac*, King Gizzard
  *Nonagon Infinity*). The host's own sockets agree: peers from `79.116.33.175`,
  `93.3.112.120`, `136.34.95.178` and `188.26.220.170` hold ESTABLISHED
  connections on the listen port.
- **From inside the owner's network, nothing can.** Their own client
  (`implosion4888`, SoulseekQt, listening on 53949/53950) could not browse the
  share, and neither could slskd browsing its own account: both are handed
  `216.212.53.255:50000` and both are refused from inside, while the same
  address accepts from outside. The router's own UPnP table shows every forward
  is there (50000 → 192.168.40.62, plus 53949/53950 for SoulseekQt) — what is
  missing is NAT loopback, and OpenWrt's UPnP daemon has no option that could
  add it, so every mapping it creates is unreachable from inside by design.
- **A browse of your own account is not a network question at all.** slskd
  already holds the index it answers browses with, so the app answers that one
  itself.

## What changed

`GET /api/soulseek/browse/<username>` answers from slskd's index when
`<username>` is your configured `soulseek_username` (R297): the same tree a peer
is served, no round trip, `local: true`, and a note the modal shows — it is this
app's own share, and a browse of it over the peer network needs the router's NAT
loopback. It is cached like a browse (48 MB of index at most, 60 s of reuse,
`refresh=1` bypasses it), and an index that cannot be read is slskd's own 502,
never an empty share. Every other username still goes over the peer network.

## What a stock install needs — nothing to script

Install the container and that is it: `docker-compose.yml` publishes the listen
port (`MLO_SOULSEEK_LISTEN_PORT`, default 50000) and the app asks the router for
the forward itself (UPnP first, NAT-PMP behind it, reported as made only when the
gateway confirms it). Peers on the internet can then search, browse and download
— measured above. What is left is the network's own business, and **Test port**
names both parts: the router has to forward the port (the app asks it to), and a
client on the SAME network needs the router to reflect its own public address —
one setting, not a host script. No host-side setup is used by this release, and
the temporary experiment used during the audit was reverted.

Verified: `python tools/test_soulseek_sharing.py` (new section — the own-account
browse answers locally and marks itself, another username still browses over the
peer network with slskd's own errors, and an unreadable index is that 502 rather
than an empty share), `npx tsc -b` and `npm run build`, the running container's
own browse of its account answered from its index, and all ten version copies at
4.1.10.
