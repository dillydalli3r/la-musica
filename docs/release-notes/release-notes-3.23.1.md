# la musica 3.23.1 — three things that looked fine and were not

Follow-ups to 3.23.0, all three reported from a running install: a share that
was serving 104 files while the card said *browsable*, a Browse sheet reading
the disk's folder spellings instead of the library's own names, and a volume
readout whose last digit was clipped by its own box.

## The sharing card no longer promises a browse it cannot make

The card's summary was `slskd is sharing N files in M folders — other users can
search, browse and download them`, and it stayed green while its own notes said
`Peers cannot connect back to this client`. Both were true and one of them was
the whole problem: search and the share index travel over the *outgoing*
connection, but a **browse is a connection back to the listen port**, and
nothing had confirmed a forward for it.

- The audit has a status for that now — `listen_unconfirmed`, ranked under the
  browse failures — raised when automatic port opening is **on** and the gateway
  answered `no_gateway` / `unsupported` / `refused` / `error`. The card reads
  *port unconfirmed*, in amber, with the port named and the gateway's own words.
- A mapping the router **confirmed** still reads `ok`, and automatic opening
  turned off is still just a note: this is about the app's request going
  unanswered, not about every install without UPnP.
- **It clears itself once peers really have reached you.** A transfer in
  slskd's upload tree is a connection a peer opened *to* the listen port, so the
  audit is `ok` again (with the count in its notes) as soon as one exists —
  otherwise a forward made by hand would leave the card amber forever, since
  nothing inside a container can read the router's mapping.
- **In a container the remedy is written for a container.** The gateway this
  process can see is Docker's bridge (`172.18.x.1`), so the automatic step can
  never reach the home router, and a router cannot forward to a container
  address. The hint (and the *Test port* panel) now say: publish the port
  (`docker-compose.yml` already does) and forward it on the router to the
  **host's** LAN address.
- Why it mattered: on the install this came from, slskd served 104 files in 7
  folders, `GET /api/soulseek/uploads` was empty and `slskd.log` carried no
  inbound connection line at all — the share was never the problem, the forward
  was, and the green card hid it.

## Browse's track sheet reads the library, not the folder names

`mlo.query` stamps each row with the artist and album **folder** basenames; two
columns were printing those stamps while the rest of the app reads the
payload: `Radiohead [a74b1b7f-…]` and `[Album] 1994-11-29 … {GB - CD …}
[Parlophone] [<mbid>]` where every other page shows *Radiohead* and *The Bends*.

- Artist and Album now lead with the payload's `album_artist || display_name`
  and its `ALBUM` tag, with the stamps kept as the fallback for a row the
  payload does not carry.
- The **Rating** column read `tr.rating` — a key the engine never stamps — and
  fell through to the file's Picard `RATING` tag, which is 0-100: a five-star
  track printed `100` in a 0-5 column while a rating given in the app printed
  `—`. It reads the app's own store now, through the same helper as every other
  row.
- The first request no longer sorts by `library.path` while `/api/library/fields`
  is still in flight (the sheet came back in file order under a header that read
  *Artist*); it asks for the key the toolbar actually shows.

## The volume readout fits its own digits

`VolumePct`'s box was a fixed 28 px with 8 px of padding — three digits need
27 px in the shipped mono font, so `100` was clipped by the input's own edge,
and worse wherever the system mono is wider or the browser's text size is
larger. The box is sized in `ch` now (`calc(3ch + 0.5rem + 2px)`): three
characters of the font it renders in, its own padding in `rem`, and the border.
Measured in the fullscreen player at a 16 / 20 / 24 px root: no clipping at any
of them (the pixel box clipped at all three). The player bar's volume line
shares the component, so both are fixed at once.
