# la musica 4.3.2 — the column list that looked deliberate

4.3.1 taught the album tracklist to reconcile a stored column list against the
columns the build ships — but its record of "what this reader chose" was itself
a fingerprint that could never go stale, and the owner's browser already had a
record written *from* the broken three-id list. So 4.3.1 saw "the reader chose
three columns" and kept them: the album page still drew `#`, COVER, Title with
seven columns shipped.

The record now states what it is meant to state — **the reader's own removal**
(`{v: 2, removed: [...], added: [...]}`), from which the drawn set is derived as
"every column this build ships by default, minus what the reader removed, plus
what they added". A record without that version (any state written by 4.3.0 or
4.3.1, including the owner's) is treated as UNKNOWN rather than as a choice:
it is reconciled once and rewritten as v2. From then on an untick is a removal
and sticks — a later build that ships a new column still shows it, which is the
same "a preference is not evidence about this build" rule the key has carried
since R320.

Reproduced against the owner's own bundle and state and verified after:
`mlo-cols4-album-tracks = ["num","cover","title"]` with a v1 record drew
`#, Cover, Title` before and draws
`#, Cover, Title, Genre, Dur, Bitrate, DR` (plus the reader's rating/action
columns) after, with the state rewritten as v2. The library's other lists are
unchanged — a seeded v2 removal of `dr` (or of `album` in the Tracks view) keeps
that column hidden across reloads, and a hostile list that merely lacks ids can
no longer hide them.

Verified: `tools/check_library_tables.cjs` PASS (72 ok, 0 fail) including the
owner's own state and the v2-removal cases; `tools/check_library_az.mjs` 93/93;
`npx tsc --noEmit -p tsconfig.app.json` and `npm run build` clean.
R320 updated with the record's rule.
