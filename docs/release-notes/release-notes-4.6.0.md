# la musica 4.6.0 — Optimize tags (script 23), and one verdict per card

## 23 scripts: the excess-tag strip gets its own button

The alias rules of 4.5.0 made the app *grade* excess the way it grades a missing
tag — and on the owner's own The Wall that immediately showed what a real
library carries: four `ARTISTALIAS` spellings (`ARTISTALIAS`, `-EN`, `-JA`,
`-KO`) on a Latin name, with the fix two scripts away and no menu entry that
says "clean the tags" on its own.

**Script 23 — Optimize tags** is that entry point. It deletes exactly what the
grader already calls excess, through the ONE predicate and the ONE stripper
script 10's own tag pass uses (`mlo.format_all.excess_tags` →
`strip_excess_tags`), so a scoped run can never leave a tag the grade flags nor
delete one it requires:

- a tag NAME outside the shared vocabulary (TAG_MAP, the encoder identity tags,
  beets/Picard's spellings, the app's own override);
- a `COMMENT` carrying a value (the grader's `tag_value_excess`);
- an alias nothing needs — unneeded, spelled for another locale, duplicated, or
  just the name again (R16a/R16b).

It **only deletes** — no value is ever written — and a write is a change: a file
with nothing excess is not written at all (measured: one container write for two
dirty files and none for the clean one). It is offered like every other script:
on the Optimization page, in the import chain (right behind 10, whose lists it
shares), and in the **library's item menus** (album, track, artist, playlist —
`scope: "file"`), which is where a user who just saw the grade warning will look
for it. No force flag, deliberately: its subject IS the excess tag, so there is
nothing for a "force" to re-do.

The grade messages that pointed at script 3 for alias excess now name script 23
(script 3 cleans the vocabulary half, not aliases), and the spec's rule list
carries R343.

## An artist page's album card states its verdict once

The card's own status dot IS the grade verdict (`statusFor(pass, audit)`), and
the artist page also passed a `GradeBadge`, so a failing album wore a red dot in
the caption and a red cross under its stars (a passing one, a green dot and a
tick) — one fact, twice, under every cover. The badge is gone from that page;
the dot's tooltip names the verdict in words, and the album page and the Library
table still state the score.

## Verified

- `tools/test_tag_hygiene.py` (111 assertions: a file with an unneeded alias is
  cleaned, a needed alias is untouched, a clean file is not written — mtimes and
  sha256 — a second library-wide run is a no-op, the switch being off is
  reported, and the grade passes afterwards), `test_script_menu.py` (23 is
  file-scoped, offered by album AND track, no forced twin, gate =
  `strip_unknown_tags`), `test_script_menus.py`, `test_script_optimizations.py`
  (one write for two dirty files, zero for the clean one), plus the full sweep.
- Script 23 run against a **copy of the owner's own track** (four alias tags
  from an older import): all four removed, `TITLE`/`ARTIST`/`ALBUM` kept, and
  the second run wrote nothing (`modified_count: 0`, the file's mtime
  unchanged).
- `GET /api/script-menu` on a scratch server: 23 scripts, `Optimize tags`,
  `scope: "file"`, in the run order behind 10.
- `npx tsc -b`, `npx oxlint`, `npm run build` clean; the artist card checked on
  a rendered page (the dot is drawn, no cross or tick in the card).
