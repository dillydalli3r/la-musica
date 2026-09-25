# la musica 4.1.6 — the column list that was ignored is repaired too

4.1.5 stopped the Library's Tracks view obeying a stored visible-columns list
that would leave it nothing but furniture — a `#` header, a column of row
numbers, no data — and drew the view's own defaults instead. It left the list
itself alone, and that is not enough: the Columns menu draws what the hook
RETURNS, so the reader saw the defaults ticked while the stored list was still
`["num"]` — and the next tick in that menu wrote from the broken list and
collapsed the table to the one column they had clicked.

`useColumnPrefs` now STORES the defaults it draws, once, when it refuses a list:
the menu, the table and the next toggle agree from that point on. The same rule
covers every table that uses it (the Library's tracks, albums and artist views,
the expanded album tracklist, the album page, downloads, favourites and trash).

Verified in `tools/check_library_az.mjs`: the seeded case — a stored
`mlo-cols4-tracks` of `["num"]` — now also reads that key back and asserts it
holds the drawn columns with the row number kept, so the repair cannot be undone
by a later edit to the guard (56/56 checks).
