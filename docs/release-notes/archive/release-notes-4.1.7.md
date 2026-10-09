# la musica 4.1.7 — the three open issues: the phone's album page, the phone's card, and the share a container cannot make reachable

Issues #55, #56 and #57, from the owner's own install and their own screenshots.

## The lyric chips: the value sits between the buttons, and the buttons do not move (#56)

Two reports on the `− value +` chips the fullscreen player's footer and the
lyrics sidebar carry.

*Centring.* The value was right-packed in its fixed 40 px box, so the number
hugged the `+` and left a hole on the `−` side: measured off the owner's
screenshot, 27 px of air against 12 px, on both chips. The box now centres its
content (`LYRIC_VALUE_BOX`), and the two step buttons read as one pair around the
value they step. The boxes are unchanged, so the `−`/`+` still land at identical
offsets from either chip's edges — the two chips are still one geometry.

*The Save that moved everything.* The Save and the Discard were rendered only
once the offset was dirty, and appending them widened the chip by their own
58 px the instant the first step landed. In the fullscreen footer — a
`justify-end` row — the whole strip then slid that far left, so the reader's next
press, aimed at the `+` they had just used, landed on Save (a write into the
track's own lyrics) or on Discard. They now live in a slot whose width is there
in every state: the two buttons are `invisible` until there is something to save,
and a press on the `+` cannot move a single box in the strip.

Measured in `tools/check_np_metadata_contrast.cjs`, which now asserts both halves
on all four stubbed covers — the value's ink centre against the midpoint of the
two glyphs, the slot's width in both states, and a real mouse press on the `+`
that must leave every box where it was. Against the previous sources the same
script reports what the owner saw, to the pixel: `zoom.x 1172.5→1112.5`,
`offset.w 100→160`, the value's ink centre 9.0 px off the midpoint. After the
change: no box moves, the slot stays 58 px, and the ink centre is within 2 px.

## The album page on a phone (#55, first bug)

"Title text in album pages looks very squished… one syllable per line." The
tracklist kept its `min-w-[814px]` floor at every width, so the table was 814 px
wide inside a 390 px screen and the Title column — the one column laid out
`w-auto`, with the heart, the rating stars and the `⋯` sharing its cell — was
left a few px. It now folds like every other table in the app below `md`
(`ALBUM_TRACK_PHONE_CLS`): genre, bitrate, dynamic range, every user-added tag
column and the cover go, and the release's own spine — the track number, the
title and the length — stays, with the title given the room to read.

`tools/check_library_tables.cjs` — which already owned table geometry — gained the
album tracklist's own section and points at a scratch library shaped like the
owner's (five tracks, titles from 5 to 55 characters, their two extra tag columns
`Rc`/`Wr`). Against the pre-fix sources it reports what the screenshot shows, in
numbers: wrapper 814/342 px inside a 390 px page, the Title column 8 px, the name
link 0 px, **50 lines of 20 px**, the row 1084 px tall, the row's own chrome laid
out *outside* the cell and the `⋯` button's hit area 0 px. After the change the
same run reads: wrapper 342/342, page 390/390 (no sideways scroll), Cover, Genre,
Bitrate, DR, Rc and Wr folded, `#`, Title, Duration and the corner control kept,
the Title column 122 px with the name link 98 px over two lines and the row
144 px, and the chrome inside the cell — heart 26×26 and `⋯` 24×24, each with the
phone's 44 px hit area. At 800 px nothing folds and the table holds its 814 px
floor with the wrapper scrolling for it; at 1440 all nine columns plus the corner
are drawn. The Library's expanded album row at 390 px (which shares the floor)
now folds `#`/Genre/Duration/Bitrate/DR and keeps the cover and the title, and
`tools/check_library_az.mjs` stays 56/56.

## The card the phone draws, and the music that stops when it is not in front (#55)

*The step, not the skip.* The owner's lock screen showed ⟲10 / 10⟳ beside this
app's own metadata, where they wanted the ⏮ ⏸ ⏭ the app itself has. A web page
is offered skip-forward/skip-backward by default — WebKit enables the pair with
its own interval whether or not the page asked — so the player now declares both
SKIP actions unsupported (`setActionHandler("seekbackward" | "seekforward",
null)`), which is the page saying the app has no such control, and leaves the
system to draw the track step the app does have. `tools/check_os_stop_resume.cjs`
reads the declared set back out of the page: previous, next, seek, play, pause
are functions; the skip pair is explicitly `null`.

*The star.* `likeCommand` is the star iOS draws on that card, and it is re-asserted
at every transition the app already knew about — but the same bit is written by
WebKit's own media-session plumbing a few milliseconds AFTER the web event that
caused our write, which is a star that vanishes mid-album. `ios_like.rs` now
asserts it again half a second later (`assert_again_soon`), once per burst, and
the app's playback readout gained the bits the system actually has
(`now_playing_like_enabled`, `now_playing_skip_back_enabled`,
`now_playing_skip_forward_enabled`) so the next report from a phone says whether
the star was pressable at that moment rather than what it looked like.
`tools/check_ios_ipa.py` asserts the new code is in the shipped IPA, so the
release build is checked, not just the sources. Which glyphs the OS finally
paints is Apple's; the set it paints from is now named, asserted and readable.

*The music that stops.* Nothing in the web player pauses on visibility, so a
`pause` that arrives while the page is hidden and that no app path caused IS the
platform stopping the track. It is now named in the playback report (`os-stop`,
with readyState, currentTime, ended, networkState and the app's last reason) and
recovered when the reader comes back: the same element is restarted once, on the
reconcile that already ran on `visibilitychange`/`pageshow`, and the outcome is
reported as `os-resume`. Every deliberate pause — the transport, the sleep timer,
the queue's end, a track change, the lock screen's own pause — clears the marker
first, so a pause the reader asked for is never undone by returning to the app.
Measured in `tools/check_os_stop_resume.cjs`: 23/23 on this build (the file gained
its own case for the declared action set with this release), 9/18 when the same
script ran against the pre-change sources — identical over three consecutive runs
— the stop resumes where it stopped with exactly one extra `play()`, nothing
stopped means no `play()` at all, and both a transport pause and a lock-screen
pause survive the round trip.

## Soulseek in a container: the third link (#57)

"Soulseek sharing refuses to work in the Docker container… the amount of files
hosted pops up on clients, but the library cannot be viewed."

The audit, run against the owner's live container, was green everywhere the app
could see: port 50000 published and listening, slskd logged in, the share live
(14 directories, 165 files), the index answering with a real tree, `browse.ok`.
The link the app could not see is the one in front of the host. Measured: the
host's and the container's internet egress is the same address
(87.249.138.224), while the router's own WAN address is 216.212.53.255, and the
host's default route with the lowest metric goes through a tunnel
(`route print -4`: `0.0.0.0/0 → 100.64.0.1 via 100.72.6.55`, metric 6, against the
NIC's metric 25). A Tailscale exit node was carrying the traffic, so the address
the Soulseek server had been handed for this client was the tunnel's — search,
login and the share statistics work over that outbound connection, and a peer
that tries to connect BACK to it can never land. The router's forward is
irrelevant while that is true.

The app now says so. `soulseek_port._egress_fact` reads the SHAPE off the routes,
which a carrier's CGNAT cannot imitate: `portmap.local_ip()` with no hint is the
interface the OS itself picks for a public destination, compared with the address
the router that would have to forward the port reaches this machine on. The same
network on both (plus the route's own next hop on it) is a line that hands this
machine a CGNAT address — `fail`, ask the ISP. Any other reading is a TUNNEL —
`fail`, naming the addresses measured, the shape, and the fix that is local:
leave the exit node off for this host, or split-route it so this app's traffic
leaves by the ISP line. Run on the owner's own host, the row now reads:

```
the mapping points at this machine (192.168.40.62). the gateway states
216.212.53.255 as its own WAN address, which is public, so a mapping there is
reachable in principle. this machine reaches the internet through a TUNNEL,
which no row above can see: the address it picks for a public destination is
100.72.6.55, in 100.64.0.0/10, while … Turn the exit node off for this host, or
split-route it … this is NOT a carrier's CGNAT, so no call to the ISP can change
it.
```

A container is skipped outright — the host's routing table is not visible from in
there — so the container-facing texts carry the check instead ("compare the
HOST's own egress with the router's WAN; the same address is the carrier's, a
different one is the tunnel's"), in the port check's mapping note, on all three
container branches of `soulseek._listen_hint`, and in the audit's own container
note. `README.md` gained *Soulseek in Docker: the three links that have to line
up* (the publish line, the router forward to the HOST's LAN address, and this
trap), `docker-compose.yml`'s port comment block names it, and the rule is
R294/§7.52. `tools/test_soulseek_port.py` is 79 checks (63 before), with both new
cases failing against the previous module; the sharing suite and every
soulseek-adjacent suite pass.

Rules: R240 amended (the chips' centred value and the reserved slot), R295
(the platform's stop is named and recovered), R296 (the declared command set and
the re-assert), R294 (the egress shape). Web builds clean, `tsc -b` clean, all
ten version copies agree at 4.1.7.
