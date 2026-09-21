import type { QueryClient } from "@tanstack/react-query";

/** Refresh every query derived from the library after a write: the library
 * listings, the home shelves, and the album/artist/track payloads that carry
 * tags and grading. Grading, tags and covers all live in more than one of
 * these caches, so a write that only refreshes its own list leaves the rest
 * of the UI stale. */
export function invalidateLibrary(qc: QueryClient): void {
  for (const queryKey of [
    ["library"],
    ["home"],
    ["album"],
    ["artist"],
    ["track-tags"],
    // The player bar and the lyrics panes read the CURRENT track's tags with
    // a 5-minute staleTime; without this a tag write leaves the playing track
    // showing its old TITLE/tech for minutes.
    ["tags"],
  ]) {
    qc.invalidateQueries({ queryKey });
  }
}

/** The cover a client most recently WROTE, per album + file name.
 *
 *  A cover's URL is its folder and file name, and replacing the image does not
 *  change either: cover.jpg is still cover.jpg, so every already-rendered
 *  `<img>` and every cache entry keeps pointing at the PREVIOUS bytes and the
 *  preview shows the old cover — or, when the first load had failed, stays
 *  empty for good.
 *
 *  A cover write's response carries a token for the file it wrote
 *  (`CoverWriteResult.token` — that file's mtime + size). Remembering it here
 *  is what makes the next render of that cover a DIFFERENT url, and therefore
 *  a fresh request, without every call site threading the token through: the
 *  query invalidation a write already does re-renders the cover, and
 *  `api.coverUrl` reads this back. Bounded — only the newest writes can still
 *  be on screen. */
const coverVersions = new Map<string, string>();
const COVER_VERSIONS_MAX = 64;

export function rememberCoverVersion(
  albumPath: string,
  coverFile: string | null | undefined,
  token: string | null | undefined
): void {
  if (!coverFile || !token) return;
  const key = `${albumPath}\u0000${coverFile}`;
  // Re-inserted, so the map's order is "oldest write first" and eviction takes
  // the entries no render can still be showing.
  coverVersions.delete(key);
  coverVersions.set(key, token);
  for (const k of coverVersions.keys()) {
    if (coverVersions.size <= COVER_VERSIONS_MAX) break;
    coverVersions.delete(k);
  }
}

/** The version a write gave this album's `coverFile`, or null when nothing has
 *  been written for it in this session. */
export function coverVersion(albumPath: string, coverFile: string): string | null {
  return coverVersions.get(`${albumPath}\u0000${coverFile}`) ?? null;
}
