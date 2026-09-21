/** The genre contract every genre-rendering screen shares with the backend.
 *
 *  A GENRE tag is a LIST of repeated fields: the FAMILY first, the SPECIFIC
 *  genres after it (`mlo/genres.py`, `mlo/genre_vocab.py`). A file's repeats
 *  read back joined with "; " (mlo/audio.py) and the rendered form joins the
 *  slots with " / " (GENRE_ORDER_SEPARATOR) — so the display is one name per
 *  chip, split on both, and never the joined string. */

/** The 28 families `mlo.genre_vocab.FAMILIES` publishes: the broad head a
 *  specific genre sits under, which the app derives from the specific genre
 *  instead of asking for it. Bundled rather than fetched — ~400 bytes, and a
 *  screen has to know which chip is the family slot before any request
 *  answers. */
export const GENRE_FAMILIES: Record<string, true> = {
  metal: true, punk: true, "hip hop": true, rock: true, pop: true, jazz: true,
  blues: true, soul: true, funk: true, "r&b": true, reggae: true, country: true,
  folk: true, classical: true, latin: true, gospel: true, industrial: true,
  experimental: true, ambient: true, "new age": true, "easy listening": true,
  dance: true, electronic: true, instrumental: true, "spoken word": true,
  comedy: true, "christmas music": true, "children's music": true,
};

/** The genre-list ceiling: `mlo.genres.GENRE_COUNT_MAX`, the range the server
 *  validates `mb_genre_count` into. Only used to clamp a stale config. */
export const GENRE_COUNT_MAX = 3;

/** The names inside one stored GENRE value. A genre name carries neither "/"
 *  nor ";" (mlo.genres._SPLIT_RE), so both separators split safely — which is
 *  what keeps a "shoegaze / rock" value from rendering as one name. */
export const splitGenres = (value: unknown): string[] =>
  String(value ?? "").split(/[/;]/).map((g) => g.trim()).filter(Boolean);

/** MusicBrainz's own spelling, as far as the browser can reproduce it.
 *
 *  The server is the authority (`mlo.genres.canonical` folds case, resolves
 *  the alias table and validates against the 2 202-name vocabulary, none of
 *  which is worth shipping to a page for this). What this reproduces is the
 *  fold — case and collapsed whitespace — which is enough to make a surface
 *  agree with `/api/genres/facets` on anything this app wrote. An alias
 *  ("synthpop" → "synth-pop") is deliberately left alone: guessing a spelling
 *  here would invent a name the server would then canonicalise differently.
 */
export const canonicalGenre = (name: unknown): string =>
  String(name ?? "").trim().replace(/\s+/g, " ").toLowerCase();

/** The derived family slot of a genre list, or null when it holds none yet.
 *  Mirrors `mlo.genres.normalize_genres`, which writes the family FIRST: a
 *  list opening with a family already carries its family slot. */
export const familyOf = (list: string[]): string | null => {
  const first = list[0];
  return first && GENRE_FAMILIES[first.toLowerCase()] ? first : null;
};
