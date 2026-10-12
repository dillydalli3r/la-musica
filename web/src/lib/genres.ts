/** The genre contract every genre-rendering screen shares with the backend.
 *
 *  A GENRE tag is a LIST of repeated fields: the TOP 2 genre names
 *  MusicBrainz itself lists for the track (MusicBrainz publishes them ranked,
 *  most-voted first), or whatever a person typed by hand — exactly those
 *  names, in that order, and nothing else. There is no derived level ABOVE a
 *  name. A file's repeats read back joined with "; " (mlo/audio.py) and the
 *  rendered form joins them with " / " — so the display is one name per chip,
 *  split on both, and never the joined string.
 *  Position is the MusicBrainz rank: the top genre first. */

/** The 28 family names `mlo.genre_vocab.FAMILIES` publishes — names a source
 *  may state in their own right, bundled rather than fetched (~400 bytes), so
 *  a screen can mark a chip as one before any request answers. */
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
