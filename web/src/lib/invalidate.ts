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
    // The grade strip on Home and the Library page (GET /api/grades/summary):
    // a run that graded, tagged or imported just changed the very checks it
    // reports, and its own staleTime would otherwise leave the strip saying
    // "3 failing" over a library that now passes.
    ["gradesSummary"],
  ]) {
    qc.invalidateQueries({ queryKey });
  }
}

/** The outcome kinds that do NOT touch the library.
 *
 *  A QUIET list rather than a loud one, deliberately: the frames that leave
 *  the library exactly as it is are these three — a newer release exists, and
 *  the two "a transfer began" halves (a Soulseek transfer moving bytes is not
 *  an album that landed) — while every other outcome in use either added an
 *  album, wrote tags, moved files or settled a wish the shelves report. A kind
 *  added later is far more likely to be about something that landed than about
 *  nothing at all, and being wrong here costs one refetch of a payload the
 *  server caches: a loud list would instead have to be maintained forever, and
 *  a forgotten kind would leave a page quietly stale.
 *
 *  `affectsLibrary` is what the App's event subscription asks (see
 *  `web/src/App.tsx`): an outcome that passes it drops the library-derived
 *  queries, which is what makes the Library and Home pages live. */
const QUIET_KINDS: Record<string, true> = {
  update_available: true,
  download_started: true,
  upload_started: true,
};

export function affectsLibrary(kind: string): boolean {
  return !(kind in QUIET_KINDS);
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
