# la musica 4.3.1 — the tracklist the reader actually has

Two fixes, both reported from a real install running the shipped build.

## A stored column list can no longer gut the tracklist

The album page drew **three** columns (`#`, COVER, Title — the title taking the
whole width) where the build ships seven, because `useColumnPrefs` obeyed any
stored list under its current key as long as one non-chrome id survived. A list
written by an older build whose ids have since changed therefore kept the three
ids that still matched and dropped the rest, and every column the reader never
unticked was simply not drawn.

`web/src/lib/columns.tsx` now keeps a record of what it last reconciled under
the current key (`mlo-coldft-<view>`): a stored list is reconciled against the
columns THIS build ships by default, a deliberate unticking is honoured once and
re-recorded, and a list that predates the id set can no longer hide the rest.
Reproduced end to end — `mlo-cols4-album-tracks = ["num","cover","title"]` on
the previous build drew `#, Cover, Title` (Title 1003 px of 1328) and draws
`#, Cover, Title, Genre, Dur, Bitrate, DR` after, with the stored list
rewritten to match. R320 (the stored-preference rule) was extended with it, and
`tools/check_library_tables.cjs` carries the four seeded states (clean /
three-ids / three-ids + legal 900 px widths / one column unticked on purpose).

**The tall rows were zoom, not layout.** The same album page measures a **52 px**
row at 100 % browser zoom and a 70 px row at 135 % — the report's screenshot was
taken at 135 %. The row height is unchanged from previous releases at any given
zoom; `tools/check_library_tables.cjs` now asserts it so a future change cannot
quietly grow it.

## A redirected run never stamps the checkout

`MLO_MUSIC_FOLDER` is an instruction about *this* run, so a run that carries it
no longer writes the repository's legacy `config.json` (`mlo/config.py`
`_write_stub`). That one line is what the NEXT process in a checkout reads —
and with a scratch install behind it, every later process answered from that
install (its password, its bind address), which is what turned "sign in
required" on for nine test suites in a row and recreated the scratch folder the
stamp pointed at. The migration contract is untouched: a stub is still written
wherever `CONFIG_FILE` points at a scratch file (the config-migration suite
asserts exactly that), and a real install that picks a folder in the UI still
records it.

## Verified

- All 128 `tools/test_*.py` suites pass in one serial run (the semantics CI
  uses), `npx tsc --noEmit -p tsconfig.app.json` and `npm run build` clean.
- `tools/check_library_tables.cjs`: the three new cases fail against the
  previous build and pass against this one; `tools/check_library_az.mjs` 93/93
  (the existing hostile-prefs contract still holds).
