# la musica 4.1.3 — sharing, measured

"Any client trying to access files shared from the app get stuck on *requesting
file list…*" — the audit that answer deserved, and one real gap closed.

## The config was right. The reachability was not, and nothing measured it.

The report looked like R279's own class (a share peers can see the size of but
never connect to — the rule that made the compose file and the daemon agree on
one number), so that hypothesis was checked first, against the live install:

* `docker port la-musica` → `8000/tcp -> 0.0.0.0:8000` and `50000/tcp -> 0.0.0.0:50000`;
* the file slskd actually reads, `/music/.mlo/data/slskd.yaml` →
  `soulseek.listen_port: 50000`, `listen_ip_address: 0.0.0.0`;
* slskd's own log → `Listening for incoming connections on 0.0.0.0:50000`;
* inside the container, `/proc/net/tcp` → `0.0.0.0:50000` LISTEN;
* `GET /api/soulseek/status` → logged in, `listen_port_state.listening: true`;
* the share itself → scan complete, 165 files in 14 folders, browse response cached.

All three numbers agree, and the listener is real — **the hypothesis is dead**.
What the log shows instead is that *nobody has ever connected back*: no inbound
connection, no upload, ever. Four external nodes (check-host.net) timed out on
`187.14.57.175:50000` while their control port answered — and `187.14.57.175` is
**this machine's NordVPN NordLynx exit**, not the home router
(`216.212.53.255`, Fidium). Peers are told an address that forwards nothing to
this machine, so a browse hangs forever while the share list (fetched from the
Soulseek server, not from us) looks perfectly healthy. The router's forward was
never in the path.

## What the app now measures (R290)

R279 keeps the compose file and the daemon from disagreeing about the *number*;
nothing measured the *publish line* itself. No container can read the host's
port list, so a missing publish line read exactly like "your router has no
UPnP" — and the card told the owner to fix both. There IS one measurement
available from inside: **a published port is accepted on the container's
gateway** (the host's DNAT hands the connection back), an unpublished one is
refused — and the app's own served port is the control, so a refusal is only
called a missing publish line on a Docker that demonstrably does hand published
ports back. Otherwise the row says it cannot be read from here and names
`docker port <container>` as the place that can.

That is the sixth row of *Test port* (`GET /api/soulseek/port-check`), and
`share_audit` consumes it: a measured miss raises its own problem code
(`listen_unpublished`, naming the exact `ports: "<port>:<port>"` line and the
`MLO_SOULSEEK_LISTEN_PORT` pin), while a measured pass clears the compose file by
evidence and leaves only what is in front of the host. The row also states the
whole inbound requirement in one line, because three reports have now guessed
wrong about it: **TCP `<port>` only — no UDP port, and no second "obfuscated"
port** (slskd implements no obfuscated route; its own config schema has no such
key and the app never writes one, so a peer that tries one falls back to this
port).

Verified with Docker end to end: published port → `ok`; unpublished port with the
control accepted → `fail` with the compose line; nothing listening → `warn`, never
a false "not published"; and against the live stack (read-only) → `ok`. Both
suites fail against the pre-fix modules restored from `git show HEAD:` and pass
after.

## What still has to happen outside the app

The app cannot fix this one, and now says so precisely:

1. **Put the forwarding and the peer-visible address on the same edge.** Either
   let slskd's traffic bypass NordVPN (turn it off for this host, or
   split-tunnel the Docker backend), or keep the VPN and use its own inbound
   port forwarding where the provider offers one. Today the exit address is the
   VPN's.
2. **Confirm the WAN is a public address, not carrier NAT** — with the VPN off,
   compare `curl https://api.ipify.org` against the router's WAN page.
3. **Forward TCP 50000 → this machine** on the router (this host is
   `192.168.40.62`). TCP only; no UDP, no second port.
4. **Allow it through Windows Firewall** (admin):
   `netsh advfirewall firewall add rule name="la-musica Soulseek 50000" dir=in action=allow protocol=TCP localport=50000`.
5. **Re-test from outside** — an external TCP check of the public address on
   port 50000 is the only proof; then *Soulseek → Test port* should read the
   publish row as ok and name only what is left.
