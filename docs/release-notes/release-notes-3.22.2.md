# la musica 3.22.2 — the sharing card that stopped moving

You reported a Soulseek share that could not be browsed and a card that said
`0.0% done`. Those are two different things, and only one of them is the app.

## The share is fine — the port is not

The app's own audit of your instance says so in one call
(`GET /api/soulseek/shares?probe=1`):

```
slskd is sharing 88 files in 6 folders — other users can search, browse and download them.
browse: "… 1-10 Motion Picture Soundtrack […].flac is in the index slskd serves"   (ok: true)
```

The index is complete, the filters are clean, the browse test passes — and the
same audit prints the reason a peer's *file list* still hangs:

```
Peers cannot connect back to this client: UPnP: no device answered the UPnP
search on 239.255.255.250:1900 — Forward the listen port on the router (or check
it on the Soulseek page) — an unforwarded listener cannot be reached from outside.
```

Soulseek *search results* flow through the server, which is why your share is
findable at all. A **browse — the file-list request — is a direct connection the
asking peer opens to YOUR listen port**, and yours is not reachable from the
internet. Your container does publish it (`0.0.0.0:50000->50000/tcp`), so what is
missing is the router: **forward TCP 50000 to this machine**, or switch UPnP on
(the app has its own mapping watcher and a **Verify browse** button on the
Soulseek page). Until then every peer sits at "Requesting file list…" forever,
which is exactly what you saw.

## The card that stopped at 0.0% — that one is ours

The shares panel fetched its audit **once**: a rescan invalidated the query, that
single answer landed while slskd had indexed a fraction of the folders, and
nothing ever asked again. The log lines printed *under* the number went on to
`Scanned 100% … Found 88 files` while the number above them stayed at
`0.0% done` for the rest of the session.

The card now polls every 1.5 s while a scan is running — and every 15 s
otherwise, the cadence the rest of that page already uses, so a scan started
behind the card's back (a save in another tab, slskd restarting, the share
watcher) is noticed too. Measured against a slskd reporting a live scan: the
card went from "slskd is indexing the shared folders (99.7% done)" to "slskd is
sharing 88 files in 6 folders" with no user action at all — six fetches in
forty-five seconds.

## And the tool you would have used to find that was broken too

`GET /api/soulseek/port-check` — the Soulseek tab's **Test port**, and the
endpoint `README.md` points a user at when a peer cannot reach the share —
answered **500** on every install. It called
`pmp_external_address(gw, …, pmp_port=…)`, a keyword that function does not take,
so the read raised

```
TypeError: pmp_external_address() got an unexpected keyword argument 'pmp_port'
```

before it could report anything at all. (That line is from your container's own
log, which is how this was found.) The call now passes the parameter the
function declares, and the no-gateway path — a router with UPnP switched off,
which is the ordinary case and exactly the one this probe exists to explain — is
pinned by a test that runs the real discovery and the real NAT-PMP client against
a closed port, nothing stubbed.

## Upgrading

Nothing to do. If peers still cannot browse the share, it is the port: forward
TCP 50000 (or enable UPnP), and the next file-list request will be answered.
