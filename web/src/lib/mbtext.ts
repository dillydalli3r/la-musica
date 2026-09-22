/** MusicBrainz text as the reader's locale names it.
 *
 *  An entity's other-language names are aliases; the SERVER picks the one the
 *  configured `locale` asks for (Settings -> Import & tags) and leaves it out
 *  when it would only repeat the name (see `server.integrations.alias_for`),
 *  so a page only adds the parentheses — everywhere a MusicBrainz name or
 *  title is rendered: the artist/release headers, the discography and result
 *  rows, the release tracklist and the top bar's search dropdown.
 */
export function withAlias(name: string | undefined, alias?: string): string {
  const base = (name || "").trim();
  const alt = (alias || "").trim();
  return alt ? `${base} (${alt})` : base;
}
