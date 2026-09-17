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
  ]) {
    qc.invalidateQueries({ queryKey });
  }
}
