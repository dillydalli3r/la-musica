#!/usr/bin/env node
/* The cover finder's contract — no framework, no browser.
 *
 * web/src/lib/coverSearch.ts is deliberately plain (no React, no DOM, no
 * fetch), so Node can load the REAL module the app ships (Node 24 strips the
 * types) and this script can drive it. What it asserts, in the order the
 * acceptance criteria name them:
 *
 *   * an album's identity resolves to ONE set of parameters — the release id
 *     when the page has one, its release group otherwise, and the artist/album
 *     tags in every case — and the automatic (on-open) search sends exactly
 *     what the Search button sends: the same URL, built by the one builder;
 *   * the automatic search waits for the source list (a request sent before it
 *     is known carries no `sources`/`country`, i.e. a different question),
 *     fires once per identity, and never fires when the caller already brought
 *     staged candidates or when there is no identity to ask about;
 *   * a request in flight is "searching", never "empty" — and the empty state
 *     is reachable ONLY from a real zero-candidate answer, which carries the
 *     terms it was asked for;
 *   * a failed request surfaces the server's or the provider's own words, and
 *     an answer that came out of the app's offline copy is marked as such (a
 *     stored zero is not a fresh "there is nothing");
 *   * an answer belongs to its query: two different questions have two
 *     different request paths, and the terms shown are the ANSWER's, not the
 *     fields as they stand after an edit.
 *
 * Run: node tools/test_cover_search_flow.cjs   (or: python tools/test_cover_search_flow.py)
 */
const path = require("node:path");

const problems = [];
const fail = (message) => problems.push(message);
const check = (label, condition) => {
  console.log(`  ${condition ? "ok  " : "FAIL"} ${label}`);
  if (!condition) fail(label);
};

(async () => {
  const mod = await import(
    new URL("../web/src/lib/coverSearch.ts", `file://${__filename}`).href
  );
  const {
    autoCoverSearch,
    coverIdentity,
    coverIdentityGaps,
    coverIdentityKey,
    coverQuery,
    coverSearchPath,
    coverSearchPhase,
    coverSearchable,
    coverTerms,
  } = mod;

  // The album the browser proof was taken on: MusicBrainz tags complete, so
  // both the release id and the release-group id are known.
  const LOVELESS = coverIdentity({
    artist: "  My Bloody Valentine ",
    album: "Loveless  ",
    releaseMbid: " 68f5b261-dcea-4506-b400-90efc4c60254",
    releaseGroupMbid: "cb76227e-3ac0-3002-9a10-615a5b73cc59",
  });
  // What /api/cover/sources answered for it (the saved defaults: none saved,
  // so the catalogue's own enabled set, capped at its limit).
  const SOURCES = ["qobuz", "applemusic", "tidal", "bandcamp", "deezer", "spotify", "itunes", "discogs", "musicbrainz"];
  const COUNTRY = "us";
  const MANUAL_PATH =
    "/cover/search?artist=My+Bloody+Valentine&album=Loveless" +
    `&sources=${SOURCES.join("%2C")}&country=us` +
    "&release_group_mbid=cb76227e-3ac0-3002-9a10-615a5b73cc59" +
    "&release_mbid=68f5b261-dcea-4506-b400-90efc4c60254";

  console.log("\nidentity");
  check("a whitespace-only tag is a missing tag", LOVELESS.artist === "My Bloody Valentine" && LOVELESS.album === "Loveless");
  check("the release id is the identity a release page has", LOVELESS.releaseMbid === "68f5b261-dcea-4506-b400-90efc4c60254");
  check(
    "with neither name the album cannot be asked at all",
    coverSearchable(coverIdentity({ releaseMbid: LOVELESS.releaseMbid })) === false &&
      coverIdentityGaps({ artist: "", album: "", releaseMbid: LOVELESS.releaseMbid, releaseGroupMbid: "" }).join(",") === "names"
  );
  check(
    "the names are searchable without any id, and the missing id is stated",
    coverSearchable(coverIdentity({ artist: "Ride", album: "Nowhere" })) === true &&
      coverIdentityGaps({ artist: "Ride", album: "Nowhere", releaseMbid: "", releaseGroupMbid: "" }).join(",") === "release"
  );
  check(
    "nothing at all is two gaps",
    coverIdentityGaps({ artist: "", album: "", releaseMbid: "", releaseGroupMbid: "" }).join(",") === "names,release"
  );
  check(
    "the group id drives the query when there is no release id",
    coverSearchPath(coverQuery(coverIdentity({ artist: "Ride", album: "Nowhere", releaseGroupMbid: "4ff18a50-33fa-37aa-a355-ea2a70e35ae3" }), { sources: SOURCES, country: COUNTRY })).includes(
      "release_group_mbid=4ff18a50-33fa-37aa-a355-ea2a70e35ae3"
    )
  );

  console.log("\nthe one builder: the automatic search asks what the button asks");
  const autoQuery = autoCoverSearch({
    identity: LOVELESS,
    sources: SOURCES,
    country: COUNTRY,
    sourcesReady: true,
    staged: false,
    asked: null,
  });
  check("the automatic search has a query once the sources are known", autoQuery !== null);
  check("...and it is exactly the Search button's query", coverSearchPath(autoQuery) === MANUAL_PATH);
  check(
    "...which is the URL the browser sent for a manual search",
    coverSearchPath(coverQuery(LOVELESS, { sources: SOURCES, country: COUNTRY })) === MANUAL_PATH
  );
  check(
    "the parameters are the identity's names, not the release id's stand-in",
    autoQuery.artist === "My Bloody Valentine" && autoQuery.album === "Loveless" && autoQuery.sources.length === SOURCES.length
  );
  check(
    "an override the finder has not got yet is omitted, never sent empty",
    coverSearchPath(coverQuery(LOVELESS)) ===
      "/cover/search?artist=My+Bloody+Valentine&album=Loveless" +
        "&release_group_mbid=cb76227e-3ac0-3002-9a10-615a5b73cc59" +
        "&release_mbid=68f5b261-dcea-4506-b400-90efc4c60254"
  );

  console.log("\nthe automatic search fires once, and late rather than wrong");
  check(
    "it does not fire before the source list is known (which would send a different question)",
    autoCoverSearch({ identity: LOVELESS, sources: [], country: "", sourcesReady: false, staged: false, asked: null }) === null
  );
  check(
    "...and it does fire — with the sources — the moment they are",
    autoCoverSearch({ identity: LOVELESS, sources: SOURCES, country: COUNTRY, sourcesReady: true, staged: false, asked: null }) !== null
  );
  check(
    "staged candidates are the answer: no second search",
    autoCoverSearch({ identity: LOVELESS, sources: SOURCES, country: COUNTRY, sourcesReady: true, staged: true, asked: null }) === null
  );
  check(
    "an identity with nothing to ask about is not searched",
    autoCoverSearch({ identity: coverIdentity({}), sources: SOURCES, country: COUNTRY, sourcesReady: true, staged: false, asked: null }) === null
  );
  check(
    "one automatic search per identity, whatever re-renders",
    autoCoverSearch({ identity: LOVELESS, sources: SOURCES, country: COUNTRY, sourcesReady: true, staged: false, asked: coverIdentityKey(LOVELESS) }) === null
  );
  check(
    "toggling a source does not fire one by itself (the picker says it applies to the NEXT search)",
    autoCoverSearch({ identity: LOVELESS, sources: ["deezer", "itunes"], country: "gb", sourcesReady: true, staged: false, asked: coverIdentityKey(LOVELESS) }) === null
  );
  const lateIdentity = coverIdentity({ artist: "Slowdive", album: "Souvlaki" });
  const late = autoCoverSearch({ identity: lateIdentity, sources: SOURCES, country: COUNTRY, sourcesReady: true, staged: false, asked: coverIdentityKey(LOVELESS) });
  check(
    "tags that arrive after the finder opened ARE searched (with the sources already in hand)",
    late !== null && late.artist === "Slowdive" && late.sources.length === SOURCES.length
  );

  console.log("\nthe three states");
  const inFlight = coverSearchPhase({
    query: coverQuery(LOVELESS, { sources: SOURCES, country: COUNTRY }),
    loading: true,
    error: null,
    answer: null,
    lastQuery: autoQuery,
  });
  check("a request in flight is 'searching'", inFlight.kind === "searching");
  check("...and the finder names what it is asking", inFlight.kind === "searching" && inFlight.query.album === "Loveless");
  check(
    "nothing asked yet is 'searching' too — never 'empty'",
    coverSearchPhase({ query: autoQuery, loading: false, error: null, answer: null, lastQuery: null }).kind === "searching"
  );

  const zeroAnswer = {
    query: autoQuery,
    results: [],
    provider: null,
    chosen: null,
    notes: ["covers.musichoarders.xyz: empty", "deezer: empty (nothing for this name)"],
    cached: null,
  };
  const empty = coverSearchPhase({ query: autoQuery, loading: false, error: null, answer: zeroAnswer, lastQuery: autoQuery });
  check("a real zero-candidate answer is 'empty'", empty.kind === "empty");
  const emptyTerms = empty.kind === "empty" ? coverTerms(empty.query).map((t) => `${t.kind}=${t.value}`) : [];
  check(
    "...and it carries WHAT was searched",
    emptyTerms.join("|") ===
      "artist=My Bloody Valentine|album=Loveless|release=68f5b261-dcea-4506-b400-90efc4c60254|group=cb76227e-3ac0-3002-9a10-615a5b73cc59|sources=9|region=US"
  );
  const edited = coverQuery(coverIdentity({ artist: "Slowdive", album: "Souvlaki" }), { sources: SOURCES, country: COUNTRY });
  const stillEmpty = coverSearchPhase({ query: edited, loading: false, error: null, answer: zeroAnswer, lastQuery: autoQuery });
  check(
    "an edited field does not relabel the answer's terms",
    stillEmpty.kind === "empty" && stillEmpty.query.album === "Loveless"
  );
  check(
    "...and the notes the sources reported come with it",
    empty.kind === "empty" && empty.notes.length === 2
  );

  const failed = coverSearchPhase({
    query: autoQuery,
    loading: false,
    error: "Error: cover search failed: no answer within 90s",
    answer: null,
    lastQuery: autoQuery,
  });
  check("a failed request is 'error'", failed.kind === "error");
  check(
    "...carrying the server's own words, verbatim",
    failed.kind === "error" && failed.message === "Error: cover search failed: no answer within 90s"
  );
  check("...and the query it failed on, for the retry", failed.kind === "error" && coverSearchPath(failed.query) === MANUAL_PATH);
  check("...not 'empty'", failed.kind !== "empty");
  const staleZero = coverSearchPhase({
    query: autoQuery,
    loading: false,
    error: null,
    answer: { ...zeroAnswer, cached: { at: 1700000000000 } },
    lastQuery: autoQuery,
  });
  check(
    "an answer that came off disk is marked (a stored zero is not a fresh one)",
    staleZero.kind === "empty" && staleZero.cached.at === 1700000000000
  );

  const blocked = coverSearchPhase({
    query: coverQuery(coverIdentity({})),
    loading: false,
    error: null,
    answer: null,
    lastQuery: null,
  });
  check("an album with no identity is 'blocked', not 'empty'", blocked.kind === "blocked");
  check(
    "...and it lists what is missing",
    blocked.kind === "blocked" && blocked.gaps.join(",") === "names,release"
  );
  // Tagged files are not the only way in: typing the artist and album is the
  // manual search this state tells the user about, and the answer to it must
  // be shown — not held back by the album's own empty tags.
  const typed = coverSearchPhase({
    query: coverQuery(coverIdentity({ artist: "My Bloody Valentine", album: "Loveless" }), { sources: SOURCES, country: COUNTRY }),
    loading: false,
    error: null,
    answer: zeroAnswer,
    lastQuery: null,
  });
  check("once typed, that same album's search is shown", typed.kind === "empty" && typed.cached === null);

  console.log("\nan answer belongs to its query");
  const other = coverQuery(coverIdentity({ artist: "My Bloody Valentine", album: "m b v" }), { sources: SOURCES, country: COUNTRY });
  check("two different questions have two different request paths", coverSearchPath(other) !== coverSearchPath(autoQuery));
  check("...so a stored answer can never be read back for the other", coverSearchPath(other) !== MANUAL_PATH);
  check("the same question always builds the same path", coverSearchPath(autoQuery) === coverSearchPath(coverQuery(LOVELESS, { sources: SOURCES, country: COUNTRY })));
  check(
    "the path IS the offline copy's key (path + query), which is what api.ts sends",
    coverSearchPath(autoQuery).startsWith("/cover/search?") && !coverSearchPath(autoQuery).includes("//")
  );

  if (problems.length) {
    console.log(`\nFAIL — ${problems.length} problem(s)`);
    for (const p of problems) console.log(`  - ${p}`);
    process.exitCode = 1;
    return;
  }
  console.log("\nPASS — one query builder, one search per identity, three states that cannot blur");
})();
