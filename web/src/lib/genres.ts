/** The genre contract every genre-rendering screen shares with the backend.
 *
 *  A GENRE tag is a LIST of repeated fields, and every one of them is a genre
 *  a SOURCE stated — the level above them (the parent genre, and its own
 *  parent up to a family head) is DERIVED by the server and never stored
 *  (`mlo/genres.py`, `mlo/genre_vocab.path_of`). A file's repeats read back
 *  joined with "; " (mlo/audio.py) and the rendered form joins them with " / "
 *  (GENRE_ORDER_SEPARATOR) — so the display is one name per chip, split on
 *  both, and never the joined string. Position means nothing: there is no
 *  family slot any more, and `/api/genres/facets` carries each name's derived
 *  `path` ("Pop › Synth-Pop") for the places that want to show it. */

/** The 28 families `mlo.genre_vocab.FAMILIES` publishes: the heads of the
 *  genre tree, which the app derives from a name instead of storing. Bundled
 *  rather than fetched — ~400 bytes, and a screen has to know which chip is a
 *  head (a name a source may state in its own right) before any request
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

/** The chain ABOVE a genre name — its parents, and never the name itself.
 *
 *  `/api/genres/facets` publishes each name's whole path, top first and ENDING
 *  ON THE NAME ("Hip Hop › Experimental Hip Hop"), because that is what
 *  `mlo.genre_vocab.path_of` answers. A surface that prints the path right
 *  beside the name therefore says the name TWICE and leaves the reader asking
 *  what the arrow is for (the owner's own question, 2026-10-12) — so every
 *  reader that shows "where this sits" next to the name itself takes the
 *  parents from here: `under Hip Hop`, `under Electronic › House`, and nothing
 *  at all for a name with no known parent (a family head, or a name outside
 *  the vocabulary). */
export function genreParents(path: unknown): string {
  const parts = String(path ?? "")
    .split("›")
    .map((part) => part.trim())
    .filter(Boolean);
  return parts.slice(0, -1).join(" › ");
}

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

/** The family a list's names sit under, or null when none of them is known.
 *
 *  A render-time convenience only, and deliberately the WEAKEST form of the
 *  server's answer: the real hierarchy is `mlo.genre_vocab.path_of`, which the
 *  server ships per name in `/api/genres/facets` (`path`). This looks at the
 *  names alone, so it can only see the head a name IS — enough to mark a chip
 *  that is itself a family, never enough to claim where a name sits. */
export const familyOf = (list: string[]): string | null => {
  const name = list.find((g) => GENRE_FAMILIES[g.toLowerCase()]);
  return name ?? null;
};
