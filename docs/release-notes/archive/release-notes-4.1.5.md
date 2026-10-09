# la musica 4.1.5 — the card stops lying, and titles wear their advisory

Two reports, measured on the install that made them.

## The Sharing card's two wrong rows (both introduced by 4.1.4's router setting)

Naming the router (`soulseek_router_ip`, 4.1.4) let the app read the router for
the first time — the mapping row went `ok` with the router's own sentence — and
two other rows then said the wrong thing about a mapping that was correct:

* **`Published by the host` fell from `ok` to `warn`.** The publish probe is
  the one measurement a container can make of the HOST's port list: connect to
  this container's gateway and see whether the port is reflected. The configured
  router was being handed to it, so the probe asked the *router* about a port
  list it does not keep. It now always dials the container's own gateway; the
  router a user names is a different machine and has nothing to say about the
  host's publish line.
* **`Addresses` read `fail`: "the mapping points at 192.168.40.62, but this
  machine is 172.18.0.3 on that network — peers would reach another device."**
  Inside a container those two addresses are SUPPOSED to differ: the router must
  forward the port to the HOST's address on its own network, while the address
  this process can see for itself is Docker's bridge. Reading that as a failure
  is the opposite of the truth once that address answers — the host publishes
  the port straight back into this very container (R279's own measurement). The
  row now judges that shape by what the app could measure about it: a read that
  reached the address on the port is a forward that lands on a listener, and it
  says whose address it is instead of accusing the mapping. The same mismatch on
  a DIRECT install is still the real failure it always was.

Verified on the live install: with the router named, `Router mapping for port
50000` reads **ok** — *"the gateway lists external port 50000 -> 192.168.40.62:50000,
and 192.168.40.62 accepts a connection there"*, which `GetSpecificPortMappingEntry`
confirms — and the verdict is no longer `fail`. `tools/test_soulseek_port.py`
gained a case for each (63 checks); both fail against the 4.1.4 module — the
publish case with `FAILED: …the address dialled is this container's own gateway
(['192.168.40.1'])`.

## The Library's Tracks view drew nothing but row numbers

"The track menu of the library is very broken … NOTHING SHOWS UP": the Tracks
table rendered a header of `#`, a column of row numbers, and no data — with the
Columns menu looking perfectly normal, because the columns the build never drew
were never offered to be unticked. It is the v3-preferences story one step
further on (see `lib/columns.tsx`): a stored visible-columns list can name ids
that survive the filter and are drawn by nobody.

The renderer now refuses a list that would leave the table nothing but furniture
— the row number and the cover, marked `chrome` on the `Col` — and draws the
view's own defaults instead. One rule in `useColumnPrefs`, so every table that
uses it is covered: the Library's tracks, albums and artist views, the expanded
album tracklist, the album page, downloads, favourites and trash. The stored
list is left alone; the next tick in the Columns menu writes one that says what
is on screen.

Verified by seeding exactly that list (`mlo-cols4-tracks = ["num"]`) in
`tools/check_library_az.mjs`: against the previous `columns.tsx` the check
measures `{"head":["#"],"cells":1}` — the owner's screenshot, to the character —
and passes after the fix (55/55).

## One row of stars per card, not two

The Home page's "Your ratings" shelf drew its own `StarRating` under each card
(`extraOf`, stars **and** value) while `AlbumCard` already drew the album's
rating underneath — the same number twice, and a reader left to work out which
row was theirs. The shelf's row is gone and the value moved onto the card's own
row (`showValue`), so every surface that draws a card says the same thing in one
line: the Library grid, the shelves, the album page. Nothing was lost — the
number that only the shelf's row carried is now on the card itself, on every
card.

Verified on the owner's own payloads (their albums, their ratings): the shelf
renders OK Computer ★★★★☆ 4.5, The Bends ★★★★½ 4.5, Nonagon Infinity ★★★★ 4 and
Kid A ★★★★ 4 — one row each, with the advisory mark intact.

## Why browsing yourself from your own LAN hangs forever

Reported as "still can't get soulseek sharing to work", with a SoulseekQt window
stuck on *Requesting file list…* while browsing the owner's own share. Measured:

| check | answer |
|---|---|
| slskd's listener, from inside the container | `127.0.0.1:50000` accepted |
| the public address `216.212.53.255:50000` **from inside the network** | **Connection refused** — this router does not hairpin |
| slskd's log | `Listening for incoming connections on 0.0.0.0:50000`, `Logged in to the Soulseek server as dillydallier` (three times, all after the VPN went off), `Share cache loaded … Sharing 14 directories and 165 files`, `Browse response cached successfully` |
| the app's own share audit | `browse.ok = true`, 15 directories |
| the port from OUTSIDE the network | open (check-host, three nodes) |

A client on the same LAN cannot reach the share through the public address, and
neither can a client running beside the server — so the owner's own SoulseekQt
test, and slskd browsing itself, can both hang while the share is perfectly
reachable to the internet. The test that means something has to come from
outside the network: a phone on mobile data (or any client whose traffic leaves
by another route) browsing `dillydallier`.

## Album titles wear their explicit / clean mark

`AdvisoryMark` — the boxed E and C beside a title — was drawn on the album page,
the player and the favourites rows, but **not on the grid cards**, which is
where the owner looked. And the album-level tag it was fed can lag the tracks
inside it: measured on their library, Evil Empire carries `ITUNESADVISORY 0` on
the album while **ten of its eleven tracks state 1**, and Hail to the Thief
carries 0 while three of fourteen say 1. One shared rule now reads an album's
advisory out of what its files state — any explicit track makes the album
explicit (what every store does with a compilation), a clean tag is only read as
clean when nothing in the album says otherwise — and the card and the album page
both use it. On their library that turns four silent covers into marked ones
(Evil Empire, Hail to the Thief, Nonagon Infinity, Rage Against the Machine) and
leaves the six albums that are clean untouched.

Verified: `tools/check_library_az.mjs` serves a library carrying both shapes —
one album explicit only through its tracks, one clean all the way down — and
asserts the mark on the card in a real browser (54/54 checks).
