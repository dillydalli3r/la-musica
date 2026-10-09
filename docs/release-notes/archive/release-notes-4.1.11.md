# la musica 4.1.11 — the share is one press away

4.1.10 taught the app to answer a browse of its own account from its own share
index, but the only thing in the UI that opened a browse was a search result —
and your own account does not come back from a search, so the answer existed and
nobody could press it.

The sharing card now carries **Browse my share**. It opens the browse modal on
the configured account and lists the folders and files a peer is served, read
from slskd's own index (R297), with the note that says so. Disabled only until
the account is known; no router setting, no NAT loopback, nothing installed on
the host — the one press works on a stock container.

Verified: `npx tsc -b` and `npm run build`, and the press itself driven in the
running app on the owner's install: the modal opened with the share's own tree
and the note.
