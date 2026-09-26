# la musica 4.1.8 — the browse that was never going to work, said out loud

Reported after 4.1.7: SoulseekQt, on the owner's own machine, sitting on
*Requesting file list…* while browsing `dillydallier` — their own share.

## What was measured on the live install

- The container publishes 50000 and slskd listens on it (`Listening for incoming
  connections on 0.0.0.0:50000`), logged in from the ISP address (the host's
  egress is `216.212.53.255` again — the exit node is gone).
- The router's forward reads back verified: `50000 → 192.168.40.62:50000`
  (OpenWRT, leased), so the hop in front of the host is real.
- **Three external nodes connected to `216.212.53.255:50000` in 0.002–0.17 s**
  (check-host.net, Bulgaria / Russia / Ukraine) — the port is reachable from the
  internet, with the listener up.
- From *inside* the LAN the same public address is **refused immediately**, while
  the host's own LAN address accepts.

So nothing in the share was broken: the client that reported the failure cannot
succeed by construction. SoulseekQt asks the Soulseek server for the peer's
address, is handed the public one, and dials it from inside the network — and a
router without NAT hairpinning (this one, and most of them) refuses exactly that
connection while the port is wide open to everyone else. slskd browsing its own
username fails the same way, with a 500 from the same cause. The test that means
something starts on another network.

## What this release adds

Nothing about sharing changed; the app now says this where the reader is
looking, instead of leaving a green card beside a hanging client:

- The port check's `self-connect` row — the row whose whole subject is "can this
  machine reach its own public address" — carries the consequence in its `cannot`
  text ("a Soulseek client on this SAME network cannot browse this share: it is
  handed the public address and dials it from inside, so its file-list request
  never arrives — while the share is browsable from anywhere else. The test that
  means something starts on another network"), and its no-WAN branch (every
  container) keeps the sentence instead of only explaining why the row cannot
  measure.
- The sharing card's container note carries the same fact, on the note that
  already names the egress trap.
- `README.md`'s *Soulseek in Docker: the three links* gains the paragraph
  ("And a client on YOUR OWN network cannot prove any of it"), including the one
  router-side setting that makes a local client work: NAT loopback / reflection.
- The spec's port-check rule says it too, with the measurements above.

Verified: `tools/test_soulseek_port.py` 81 checks (both new cases fail against
4.1.7's module), `tools/test_soulseek_sharing.py` green with the audit-note
assertion, all ten version copies at 4.1.8.
