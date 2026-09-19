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
