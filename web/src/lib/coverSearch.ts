/** The cover finder's own logic — which album identity a search is asked
 *  about, the exact question the API is sent, what the finder has to say when
 *  there is nothing to ask about, and the states an answer can put it in.
 *
 *  A plain module on purpose (no React, no DOM): `web/src/components/
 *  CoverSearchModal.tsx` is its only caller, and `tools/test_cover_search_flow.py`
 *  drives THIS — the parameters the automatic search and the manual button
 *  send are built in one place, and the state each answer produces is decided
 *  in one place, so both can be checked without a browser.
 *
 *  Why it exists: the finder's automatic search on open used to send a
 *  DIFFERENT question from the one the Search button sends (it fired before
 *  the source list was known, so it carried no `sources` and no `country`),
 *  and “No covers found for this query.” was shown for anything that was not a
 *  zero-candidate answer — including an answer from the offline copy, and a
 *  set of staged candidates that was empty. One builder and one state function
 *  are what make those impossible rather than unlikely. */

import type { CoverResult } from "../types";

/** The album's identity: the names the page holds and the MusicBrainz ids it
 *  knows. A release id when the album has one, its release group otherwise,
 *  and the artist/album tags in every case — the name-based sources are asked
 *  regardless, and only the ids decide what the Cover Art Archive can be asked
 *  about (its release's own front cover first, then the group's stand-in). */
export interface CoverIdentity {
  artist: string;
  album: string;
  releaseGroupMbid: string;
  releaseMbid: string;
}

const clean = (v: string | null | undefined): string => (v ?? "").trim();

/** Trim every part and drop the blanks: a whitespace-only tag is a MISSING
 *  tag, not a search term. */
export function coverIdentity(input: {
  artist?: string | null;
  album?: string | null;
  releaseGroupMbid?: string | null;
  releaseMbid?: string | null;
}): CoverIdentity {
  return {
    artist: clean(input.artist),
    album: clean(input.album),
    releaseGroupMbid: clean(input.releaseGroupMbid),
    releaseMbid: clean(input.releaseMbid),
  };
}

/** One search's question: the identity plus the per-search overrides (the
 *  source picker and the region dropdown). */
export interface CoverQuery extends CoverIdentity {
  sources: string[];
  country: string;
}

/** The ONE builder of a search's parameters. The automatic (on-open) search
 *  and the manual Search button both come through here, so the two can only
 *  ever ask the same question. */
export function coverQuery(
  identity: CoverIdentity,
  opts?: { sources?: string[] | null; country?: string | null }
): CoverQuery {
  return {
    ...coverIdentity(identity),
    sources: (opts?.sources ?? []).map((s) => clean(s)).filter(Boolean),
    country: clean(opts?.country),
  };
}

/** The request's own path and query, with no server base — the one place the
 *  question's SHAPE is decided (which parameters, in which order, and which of
 *  them are omitted when empty). `api.coverSearch` sends exactly this string,
 *  and it is also the key the app's offline copy files an answer under (see
 *  `offlineCache.cacheKey`: path + query), so an answer can never be read back
 *  for a different question. */
export function coverSearchPath(q: CoverQuery): string {
  const p = new URLSearchParams({ artist: q.artist, album: q.album });
  if (q.sources.length) p.set("sources", q.sources.join(","));
  if (q.country) p.set("country", q.country);
  if (q.releaseGroupMbid) p.set("release_group_mbid", q.releaseGroupMbid);
  if (q.releaseMbid) p.set("release_mbid", q.releaseMbid);
  return `/cover/search?${p}`;
}

/** The identity as one string: what "this identity has already been asked"
 *  is keyed by. The source list and the region are NOT part of it — the
 *  picker says a change there applies to the NEXT search, so toggling a source
 *  must never fire one by itself. */
export function coverIdentityKey(i: CoverIdentity): string {
  return [i.artist, i.album, i.releaseGroupMbid, i.releaseMbid].join("\u0000");
}

export type CoverTermKind = "artist" | "album" | "release" | "group" | "sources" | "region";

/** One part of what was searched — the labels a "what was searched" line is
 *  built from. */
export interface CoverTerm {
  kind: CoverTermKind;
  value: string | number;
}

/** What a query is asking, in the order it is sent. The empty state's line is
 *  built from the ANSWER's query, never from the fields as they stand now:
 *  after an edit those describe a different question. */
export function coverTerms(q: CoverQuery): CoverTerm[] {
  const terms: CoverTerm[] = [];
  if (q.artist) terms.push({ kind: "artist", value: q.artist });
  if (q.album) terms.push({ kind: "album", value: q.album });
  if (q.releaseMbid) terms.push({ kind: "release", value: q.releaseMbid });
  if (q.releaseGroupMbid) terms.push({ kind: "group", value: q.releaseGroupMbid });
  if (q.sources.length) terms.push({ kind: "sources", value: q.sources.length });
  if (q.country) terms.push({ kind: "region", value: q.country.toUpperCase() });
  return terms;
}

export type CoverIdentityGap = "names" | "release";

/** What this album cannot be asked about. `names` is the blocking one: every
 *  source the finder has is asked by artist/album, and the API refuses a
 *  request with neither — so a search without them can only return noise or
 *  nothing, which is exactly the answer that must not be dressed up as "no
 *  covers found". `release` is not blocking (the names are enough); it is said
 *  because without an id the Cover Art Archive's own front cover is out of
 *  reach, and that is what the album would have if it were tagged. */
export function coverIdentityGaps(i: CoverIdentity): CoverIdentityGap[] {
  const gaps: CoverIdentityGap[] = [];
  if (!i.artist && !i.album) gaps.push("names");
  if (!i.releaseMbid && !i.releaseGroupMbid) gaps.push("release");
  return gaps;
}

/** Is there anything to ask at all? */
export function coverSearchable(i: CoverIdentity): boolean {
  return Boolean(i.artist || i.album);
}

/** Whether the finder should ask its OWN question now, and what that question
 *  is; `null` means "not now". Every `null` is one of four decisions, in this
 *  order:
 *
 *    * the caller brought candidates (the import staged them) — they ARE the
 *      answer to this query and searching again would throw them away;
 *    * the source list is not known yet — asking before `/api/cover/sources`
 *      has answered sends a DIFFERENT question from the one the Search button
 *      sends (no `sources`, no `country`), which is the divergence this exists
 *      to prevent;
 *    * this identity has already been asked — ONE automatic search per
 *      identity, whatever a re-render or StrictMode's doubled effect does;
 *    * there is nothing to ask about (see `coverIdentityGaps`) — the finder
 *      says what is missing instead of searching for nothing. */
export function autoCoverSearch(input: {
  identity: CoverIdentity;
  sources: string[];
  country: string;
  /** False until `/api/cover/sources` has answered (or failed). */
  sourcesReady: boolean;
  /** True when the caller already brought candidates to show. */
  staged: boolean;
  /** The identity already asked automatically, as `coverIdentityKey`. */
  asked: string | null;
}): CoverQuery | null {
  if (input.staged) return null;
  if (!input.sourcesReady) return null;
  if (!coverSearchable(input.identity)) return null;
  if (input.asked === coverIdentityKey(input.identity)) return null;
  return coverQuery(input.identity, { sources: input.sources, country: input.country });
}

/** One answer: the candidates, the policy's own pick and notes, and the query
 *  they are the answer TO. */
export interface CoverAnswer {
  query: CoverQuery;
  results: CoverResult[];
  provider: string | null;
  chosen: CoverResult | null;
  notes: string[];
  /** Set when the answer came out of the app's offline copy instead of the
   *  server — `at` is that copy's write time, null when the service worker
   *  answered and the copy's own age is unknown. `null` means the server
   *  answered: an answer from disk is not a fresh zero-result, and the finder
   *  says which it is showing. */
  cached: { at: number | null } | null;
}

export type CoverSearchPhase =
  | { kind: "searching"; query: CoverQuery }
  | { kind: "blocked"; gaps: CoverIdentityGap[] }
  | { kind: "error"; message: string; query: CoverQuery | null }
  | { kind: "empty"; query: CoverQuery; notes: string[]; cached: { at: number | null } | null }
  | { kind: "ready"; answer: CoverAnswer };

/** The finder's state, decided in ONE place and in ONE order, so the things a
 *  user must be able to tell apart never blur:
 *
 *    blocked    there is no identity to ask about — said, not searched;
 *    error      the request failed, carrying the server's or the provider's
 *               own words, never rendered as "no covers found";
 *    searching  a request is in flight, or is about to be — never "empty";
 *    empty      a REAL answer that held no candidates, with the terms it was
 *               asked for;
 *    ready      a real answer with candidates.
 *
 *  A zero-candidate answer is the only route to `empty`: it takes an `answer`
 *  whose results are empty. Nothing else in the finder may claim "none found".
 *
 *  `query` is what the Search button would ask RIGHT NOW — the fields as they
 *  stand — so the album's empty tags block the finder until the user types
 *  something, and do not block an answer they then asked for. */
export function coverSearchPhase(state: {
  query: CoverQuery;
  loading: boolean;
  error: string | null;
  answer: CoverAnswer | null;
  /** The query the last attempt asked — what an error and an in-flight state
   *  are ABOUT (it may differ from `query` after the user edits the fields). */
  lastQuery: CoverQuery | null;
}): CoverSearchPhase {
  if (!coverSearchable(state.query)) {
    return { kind: "blocked", gaps: coverIdentityGaps(state.query) };
  }
  if (state.error !== null) {
    return { kind: "error", message: state.error, query: state.lastQuery };
  }
  if (state.loading || state.answer === null) {
    return { kind: "searching", query: state.lastQuery ?? state.query };
  }
  if (state.answer.results.length === 0) {
    return { kind: "empty", query: state.answer.query, notes: state.answer.notes, cached: state.answer.cached };
  }
  return { kind: "ready", answer: state.answer };
}
