# la musica 4.9.3 - one artist, however its albums are spelled

A library tagged by hand — or by two different tools — holds the same artist
spelled more than one way: four albums tagged "System of a Down", a fifth tagged
"System Of A Down". The Library's **Grid** and **Albums** views grouped albums on
the raw tag, so that artist was drawn as **two** artists: two headers, its albums
split across them, and the odd one out filed under a name no other row on the
page used.

- The group key is the app's own name fold now — the one the A–Z rail already
  files names under — so a name is one name everywhere. "System Of A Down",
  "SYSTEM OF A DOWN" and "system of a down" are the same artist, and so is an
  accented spelling of it.
- The header over a group is the artist **folder's** spelling: the name the
  Artists view, Home's shelf and the artist page already draw. Which spelling
  wins no longer depends on the order the albums happened to sort in.
- An album's own artist caption keeps its tag, deliberately: the self-titled
  album still reads "System Of A Down" on its card, because that is what the
  file says.
- Both views are built from ONE grouping, so the Grid and the table cannot
  disagree about which artist an album belongs to.

Nothing else moved. The name box, the A–Z rail and search already folded case,
and every other surface — the Artists view, Home's shelves, the artist page and
the album page's local shelf — already grouped by the artist's folder.

`tools/check_library_az.mjs` carries the case: one artist folder whose second
album is tagged in capitals, driven through both views, and the check fails
against the old grouping (two headers, one card under the second) and passes
against this one.
