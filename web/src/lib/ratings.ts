import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, replyFor, type RatingsPayload, type RatingsScope } from "../api";
import { toast } from "../store";
import type { Track, TrackTags } from "../types";

/** Ratings, web side — three SCOPES over one store.
 *
 *  THE CONVERSION LIVES HERE AND NOWHERE ELSE. Three scales describe the same
 *  half-star, and exactly one of them is the wire format:
 *
 *    UI + <StarRating> : 0 … 5,  steps of 0.5   (what a component passes in)
 *    API + SQLite      : 0 … 10, integers       (= Math.round(v * 2))
 *    the RATING tag    : 0 … 100 (Picard's)     (= the integer × 10, server side)
 *
 *  A WEB rating (WEBRATING / ALBUMWEBRATING, script 24) is the fourth: written
 *  by that script on the same 0-100 Picard scale, read back here as UI units
 *  (`/20`, see webRatingOf) and drawn dimmer than the user's own stars, because
 *  it is somebody else's verdict.
 *
 *  Every hook below TAKES and RETURNS UI values (0-5): a component never sees
 *  the integer, and the wire never carries a float. `toApi` is the write side
 *  (`Math.round(v * 2)`), `toUi` the read side (`/2`), and `ratingOf` is what
 *  a page uses after ONE map request.
 *
 *  A SCOPE names what a rating is about — a track (a file, keyed by its path),
 *  an album (its folder) or an artist (their folder) — and the three are
 *  INDEPENDENT rows in the store. An album or artist rating is the user's
 *  verdict on that entity, NOT the average of its tracks: the album header
 *  draws both, labelled. Every hook takes the scope (defaulting to `track`, so
 *  a caller written before the folder scopes existed is unchanged) and caches
 *  per scope, so one surface can never read another's value.
 *
 *  A rated TRACK is one row in the app's own SQLite table, keyed by the
 *  normalized track path per user and carrying the MUSICBRAINZ_TRACKID, so a
 *  moved file is healed by the same resolve as favourites (server/mbresolve.py).
 *  A file that only carries its own `RATING` tag (a Picard-tagged library, or
 *  this app before its table existed) reports that value from the server and
 *  never overwrites a stored one. Folder scopes carry no tag at all: they live
 *  in the store alone, which the header control says in its tooltip. */

/** Five stars, half-star steps. */
export const MAX_RATING = 5;

/** The one sentence every FOLDER-scope star control carries in its tooltip: an
 *  album or artist rating is the user's verdict on that entity (NOT an average
 *  of what it holds) and it lives in the app's own store, because a folder has
 *  no file to carry a RATING tag. Defined once so the four surfaces that draw
 *  the control — the album header, the artist header and the two library album
 *  rows — can never describe it differently. */
export const FOLDER_RATING_NOTE =
  "Stored in the app's database — a folder has no file to carry a RATING tag, and the value is your own verdict on the entity, never an average of what it holds";

/** UI (0-5, halves) → API (0-10, integer). Rounds to the nearest half-star
 *  so a computed value (an album average, a scaled card) still lands on the
 *  grid the DB accepts; clamps so no caller can post out of range. */
export function toApi(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(10, Math.round(value * 2)));
}

/** API (0-10) → UI (0-5, halves). Anything unset/absent reads as 0. */
export function toUi(rule: number | undefined | null): number {
  if (!rule || !Number.isFinite(rule)) return 0;
  return Math.max(0, Math.min(10, Math.round(rule))) / 2;
}

/* ---------------------------------------------------------------------- *
 * WEB ratings — what script 24 fetched. Picard's 0-100 scale, riding the
 * file's own tags; the web's verdict, NOT the user's, which is why every
 * surface draws these in a different tone (components/StarRating).
 * ---------------------------------------------------------------------- */

/** The value/sources tag pair script 24 writes for the TRACK, and the one it
 *  writes for the whole ALBUM (the same pair on every file of the album).
 *  Named once here, so no call site can read the album's value as a track's. */
export const WEB_TAGS = { value: "WEBRATING", sources: "WEBRATING_SOURCE" } as const;
export const ALBUM_WEB_TAGS = { value: "ALBUMWEBRATING", sources: "ALBUMWEBRATING_SOURCE" } as const;

/** One web rating as a surface draws it: the value in <StarRating>'s units
 *  (0-5) plus the names that contributed it. */
export interface WebRating {
  /** 0-5, the web's own number kept whole (4.4 stays 4.4 — the drawn stars
   *  snap to the control's half-star grid, the readout beside them does not). */
  value: number;
  /** The `*_SOURCE` tag as the script joined it ("; "-separated), or undefined
   *  when it recorded nothing: the tooltip then says so outright instead of
   *  implying a source answered and its name was lost. */
  sources?: string;
}

/** Picard's 0-100 rating in this app's star units (0-5) — the ONE step between
 *  those two scales, beside toApi/toUi. `undefined`, never 0, for a tag that is
 *  absent, blank or unreadable: a surface must be able to tell "the web has not
 *  rated this" from "the web rated it zero" apart, and only the former draws
 *  nothing. */
export function webRatingOf(raw: unknown): number | undefined {
  if (raw === undefined || raw === null || raw === "") return undefined;
  const n = typeof raw === "number" ? raw : Number(String(raw).trim());
  if (!Number.isFinite(n) || n <= 0) return undefined;
  return Math.min(100, n) / 20;
}

/** The `*_SOURCE` tag as a tooltip wants it: the trimmed names, or undefined
 *  when the tag is absent or blank. */
export function webSourcesOf(raw: unknown): string | undefined {
  const s = typeof raw === "string" ? raw.trim() : "";
  return s || undefined;
}

/** A TRACK's web rating, off its own tags. Read from the payload the surface
 *  already holds — nothing here fetches: the script writes the tags, and this
 *  only says what they mean. */
export function trackWebRating(tags: TrackTags | null | undefined): WebRating | undefined {
  const value = webRatingOf(tags?.[WEB_TAGS.value]);
  if (value === undefined) return undefined;
  return { value, sources: webSourcesOf(tags?.[WEB_TAGS.sources]) };
}

/** An ALBUM's web rating. `ALBUMWEBRATING` is written to EVERY file of the
 *  album, so the first track carrying one states the album's value — and the
 *  sources the same write put beside it. An album no file of which carries one
 *  has NO web rating: undefined, never a zero. */
export function albumWebRating(tracks: readonly Track[] | null | undefined): WebRating | undefined {
  for (const tr of tracks ?? []) {
    const value = webRatingOf(tr?.tags?.[ALBUM_WEB_TAGS.value]);
    if (value === undefined) continue;
    return { value, sources: webSourcesOf(tr?.tags?.[ALBUM_WEB_TAGS.sources]) };
  }
  return undefined;
}

/** One web rating as the props `<StarRating>` takes, so a call site is one
 *  spread and the value can never be handed over without the sources that
 *  came with it. `webKind` stays the caller's business: only it knows whether
 *  the value is the track's or the album's. */
export function webStarProps(web: WebRating | undefined): { webValue?: number; webSources?: string } {
  return web ? { webValue: web.value, webSources: web.sources } : {};
}

/** The query key of one scope. The scope is PART of the key — three maps, three
 *  cache entries — so a star drawn for an album can never come from the track
 *  with the same path, and no surface pays for a scope it does not draw. Every
 *  key starts with "ratings", so `invalidateQueries({ queryKey: ["ratings"] })`
 *  still covers all three. */
export const RATINGS_KEY = ["ratings", "track"] as const;

/** One scope's map (one request per page and scope, shared by every row
 *  through the query cache) plus the per-value counts. Pages pass
 *  `data.ratings` to `ratingOf`. */
export function useRatings(scope: RatingsScope = "track") {
  return useQuery({
    queryKey: ["ratings", scope] as const,
    queryFn: () => api.ratings(undefined, scope),
    staleTime: 30_000,
  });
}

/** One track's rating in UI units (0-5). Tolerates the slash variants a path
 *  arrives with — a library payload uses forward slashes, a DB row on Windows
 *  may not, and the same helper the other path-keyed maps use settles it. */
export function ratingOf(map: Record<string, number> | undefined, path: string | null | undefined): number {
  if (!map || !path) return 0;
  return toUi(replyFor(map, path));
}

/** The map with one path moved to `apiValue`, counts kept in step. Returns the
 *  SAME object when nothing changes, so an optimistic write that is a no-op
 *  does not re-render every row on the page. Pure — the tests and both
 *  mutations below go through it. */
export function applyRating(
  payload: RatingsPayload | undefined,
  path: string,
  apiValue: number
): RatingsPayload | undefined {
  if (!payload || !path) return payload;
  const from = payload.ratings?.[path] ?? 0;
  if (from === apiValue) return payload;
  const ratings = { ...payload.ratings, [path]: apiValue };
  const counts = { ...payload.counts };
  const shift = (value: number, delta: number) => {
    if (value <= 0) return;
    const key = String(value);
    const next = (counts[key] ?? 0) + delta;
    if (next > 0) counts[key] = next;
    else delete counts[key];
  };
  shift(from, -1);
  shift(apiValue, 1);
  return { ratings, counts };
}

/** Rate one entity of `scope`. The map is updated BEFORE the request (the
 *  star answers the click immediately) and only THIS path is rolled back if
 *  the server refuses — another entity may be rated while this one is in
 *  flight.
 *
 *  Failures are reported here, with the server's own message (the API's
 *  `detail`), so the promise never rejects and a caller can fire-and-forget
 *  from an onClick — a folder the library does not know answers with its own
 *  sentence. `pending(path)` is what <StarRating> needs to refuse a second
 *  write for the same element mid-flight. */
export function useSetRating(scope: RatingsScope = "track") {
  const qc = useQueryClient();
  const flight = useRef(new Set<string>());
  const [, bump] = useState(0); // re-render so pending(path) reflects the flight set

  const setRating = useCallback(
    async (path: string, value: number): Promise<void> => {
      if (!path) return;
      const key = ["ratings", scope] as const;
      const next = toApi(value);
      const before = qc.getQueryData<RatingsPayload>(key);
      const had = before?.ratings?.[path] ?? 0;
      if (before && had === next) return; // already there: no request
      flight.current.add(path);
      bump((n) => n + 1);
      qc.setQueryData<RatingsPayload>(key, (old) => applyRating(old, path, next));
      try {
        const r = await api.setRating(path, next, scope);
        // The rating is stored whatever the file did; a tag the file refused
        // is news about the FILE, so it warns without rolling anything back.
        // `skipped` (write_rating_tags off) stays silent — it is a setting,
        // not a failure — and a folder scope has no tag at all (`tag` null).
        if (r.tag?.error) toast(`Rated, but the RATING tag was not written: ${r.tag.error}`, "error");
      } catch (e) {
        qc.setQueryData<RatingsPayload>(key, (old) => applyRating(old, path, had));
        toast(e instanceof Error ? e.message : String(e), "error");
      } finally {
        flight.current.delete(path);
        bump((n) => n + 1);
      }
    },
    [qc, scope]
  );

  /** True while a write for THIS path is in flight (per-path, so a page of
   *  rows only locks the row that is actually saving). */
  const pending = useCallback((path: string | null | undefined): boolean => !!path && flight.current.has(path), []);

  return { setRating, pending };
}

/** Rate many TRACKS at once (a selection, a whole album's tracks, a playlist)
 *  — the many-FILES call, and the API's bulk route is tracks-only for exactly
 *  that reason: a selection of files names no album or artist folder, whose
 *  rating is one verdict made through `useSetRating("album" | "artist")`.
 *  Same optimistic map, rolled back per path; the reply's `failed` rows do not
 *  fail the call, so a partly-written batch is reported as it stands. The
 *  map is re-read once afterwards: a bulk reply carries counts and failures,
 *  never the map itself. */
export function useBulkRating() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ paths, rating }: { paths: string[]; rating: number }) => api.bulkRating(paths, toApi(rating)),
    onMutate: ({ paths, rating }) => {
      const next = toApi(rating);
      const before = paths.map((path) => [path, qc.getQueryData<RatingsPayload>(RATINGS_KEY)?.ratings?.[path] ?? 0] as const);
      qc.setQueryData<RatingsPayload>(RATINGS_KEY, (old) => paths.reduce((acc, path) => applyRating(acc, path, next), old));
      return { before };
    },
    onError: (e, _vars, ctx) => {
      if (ctx) {
        qc.setQueryData<RatingsPayload>(RATINGS_KEY, (old) =>
          ctx.before.reduce((acc, [path, value]) => applyRating(acc, path, value), old)
        );
      }
      toast(e instanceof Error ? e.message : String(e), "error");
    },
    onSuccess: (r) => {
      // A file the rating could not be STORED for (read-only share, vanished)
      // is per-row news, not a failed batch...
      if (r.failed?.length) {
        const first = r.failed[0];
        toast(`${r.updated} rated, ${r.failed.length} failed — ${first.error || first.path}`, "error");
      }
      // ...and a tag the file refused is the same kind of news: the rating is
      // in, the file simply does not carry it.
      if (r.tags_failed?.length) {
        const first = r.tags_failed[0];
        toast(`${r.tags_failed.length} file(s) could not be tagged — ${first.error || first.path}`, "error");
      }
      qc.invalidateQueries({ queryKey: RATINGS_KEY });
    },
  });
}
