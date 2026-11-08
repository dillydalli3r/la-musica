/** MusicBrainz text as the reader's locale names it.
 *
 *  An entity's other-language names are aliases; the SERVER picks the one the
 *  configured `locale` asks for (Settings -> Import & tags) and leaves it out
 *  when it would only repeat the name (see `server.integrations.alias_for`),
 *  so a page only adds the parentheses — everywhere a MusicBrainz name or
 *  title is rendered: the artist/release headers, the discography and result
 *  rows, the release tracklist and the top bar's search dropdown.
 *
 *  The alias is the PRIMARY caption, because it is the name the reader asked
 *  to read; the entity's own name (usually its original-language spelling) is
 *  the parenthetical: `Hikaru Utada (宇多田ヒカル)`, not the reverse. The
 *  server's readability ladder has already dropped an alias that would only
 *  repeat the name, so this never renders "X (X)".
 */
export function withAlias(name: string | null | undefined, alias?: string | null): string {
  const base = (name || "").trim();
  const alt = (alias || "").trim();
  if (!alt || !base || alt === base) return base || alt;
  return `${alt} (${base})`;
}
