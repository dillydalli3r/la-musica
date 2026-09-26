#!/usr/bin/env node
/* The release-choice panel, rendered for real.
 *
 * `/api/mb/release-choice` (server/api_choice.py over mlo/release_choice.py)
 * is one payload; this check renders the ACTUAL component with it — through
 * Vite, in memory, no build output — and asserts what a user would see: which
 * edition the app is about to fetch, the facts that decided it (each with the
 * policy rule behind it), how many editions were ranked, and the override a
 * user can press. The ranking itself is the server's (tools/test_release_choice.py
 * owns that, and the payload's shape); this is the human-facing half.
 *
 * Six states, because those are the states a user meets:
 *   - the policy's pick       -> one compact line, ranked list closed
 *   - a forced pick           -> "your pick", the forced reason, the way back
 *   - a refused pick          -> eligible=false on the PICK: the panel must
 *                               promise nothing and quote the rule that skips it
 *   - the wrong kind          -> chosen null with editions ranked: the type
 *                               filter was what found nothing, so say that
 *   - no editions at all      -> chosen null, nothing ranked
 *   - a payload from a file   -> the SERVER's own answer (`node … <payload.json>`),
 *                               asserted for the same contract, so a real
 *                               response and this fixture cannot drift apart
 *
 * And the artist page's release-group TYPE derivation —
 * `web/src/lib/artistReleaseGroups.ts`, which the page's filter menu, its
 * collapsible sections and its per-type action rows all read — is asserted on
 * the decisions themselves, off one discography fixture: Album/EP/Single lead
 * and the long tail is one bucket ordered by how many groups each type holds,
 * the leaders start open while the bucket starts folded, and the menu's box
 * finds a compound label ("Album + Live") by word, by whole spelling and with
 * spaces and case ignored.
 *
 * Both MARKUP halves ride the same harness: the artist page above, and the
 * release-group page's editions table — its header row read in ORDER (… Country
 * · Cat # · Barcode, the catalog number immediately before the bar code), a
 * row's own catalog numbers joined with " + " and readable whole in the cell's
 * tooltip, one Cat # cell per edition, and the em dash the Date column prints
 * beside an edition that states none. The number needs nothing at the source:
 * `release_group_browse` already reads each release's `label-info` through
 * `mlo.release_choice.catalog_numbers`.
 *
 * Run:  node tools/check_release_choice.mjs [payload.json]
 * Exit codes: 0 pass, 1 a check failed (the missing ones are printed), 2 the
 * environment cannot run it (no web/node_modules).
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
// Any payload paths given are rendered as their own states: `node … a.json
// b.json` proves the panel against real answers, not just the fixture below.
const payloadPaths = process.argv.slice(2);
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[choice] SKIP: web/node_modules is missing — run npm install in web/");
  process.exit(2);
}

// Loaded FROM web/ so the harness and the component share ONE React instance:
// the component's own imports are externalized by Vite and resolve to the
// project's own node_modules, and Node caches a module by resolved path — two
// copies of React would break every hook in the component.
const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
// react-query is imported as the ESM file the COMPONENT's own import resolves
// to (package exports: "import" -> build/modern/index.js, "require" ->
// build/modern/index.cjs — two files, i.e. two providers, and the component's
// useQuery would find no client). Same URL, same module instance.
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

// The app is a browser app: its store reaches for localStorage at import time.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;

/* The contract's shape, with four editions a policy actually ranks apart: the
 * original French CD it picks, a same-year vinyl (the medium order decides), a
 * later "deluxe" digital (more tracks, but not the original edition) and a
 * one-track promo. The reason strings are the engine's own words, including
 * the one it puts FIRST on an edition an unattended download would refuse. */
const RG = "b1392450-e666-3926-a536-22c65f834433";
const CD = "f0f4b0f5-2f1a-3f8f-9d1e-1c6b3b6d5f01";
const VINYL = "cccc1b0f5-2f1a-3f8f-9d1e-1c6b3b6d5f04";
const DIGITAL = "bbbb1b0f5-2f1a-3f8f-9d1e-1c6b3b6d5f03";
const PROMO = "aaaa1b0f5-2f1a-3f8f-9d1e-1c6b3b6d5f02";
const REFUSAL = "auto-import never takes a promotional or bootleg edition (auto_import_avoid_promo)";

const EDITIONS = [
  { release_mbid: CD, title: "Homework", date: "1997-01-20", country: "FR", status: "Official",
    media: ["CD"], track_count: 16, disambiguation: "", score: 0.9531, eligible: true,
    reasons: ["CD — preferred medium (order 1)", "16/16 tracks of the release group",
              "official release", "original release date 1997-01-20"] },
  { release_mbid: VINYL, title: "Homework", date: "1997-01-20", country: "GB", status: "Official",
    media: ["Vinyl"], track_count: 16, disambiguation: "", score: 0.7412, eligible: true,
    reasons: ["Vinyl — medium 3 in the configured order", "16/16 tracks of the release group",
              "official release"] },
  { release_mbid: DIGITAL, title: "Homework", date: "2007-04-03", country: "XW", status: "Official",
    media: ["Digital Media"], track_count: 17, disambiguation: "deluxe", score: 0.6188, eligible: true,
    reasons: ["Digital Media — medium 2 in the configured order", "17/16 tracks of the release group",
              "official release", "later edition: 2007-04-03 vs the original 1997-01-20"] },
  { release_mbid: PROMO, title: "Homework", date: "1997-01-20", country: "US", status: "Promotion",
    media: ["CD"], track_count: 1, disambiguation: "1-track promo", score: 0.4211, eligible: false,
    reasons: [REFUSAL, "CD — preferred medium (order 1)", "1/16 tracks of the release group",
              "promotion release"] },
];
const GROUP = { title: "Homework", first_release_date: "1997-01-20", primary_type: "Album",
                secondary_types: [], track_count: 16 };
const POLICY = {
  medium_order: ["CD", "Digital Media", "Vinyl", "Cassette", "Other"],
  preferred_country: "", prefer_original_edition: true,
  status_order: ["official", "promotion", "bootleg"],
  rules: [
    "official beats promotion beats bootleg; an unofficial release is only chosen when nothing official exists",
    "the configured medium order decides first (CD, then other physical, then digital)",
    "a release short of the release group's track count is penalised",
    "the original edition beats a later reissue unless the later one is materially more complete",
    "prefer_release_country only breaks ties",
    "prefer_original_edition prefers the explicit/original edition over a clean/edited one",
    "a plain release beats a parenthesised/disambiguated one when everything else ties",
  ],
};
const answer = (chosen, editions, group = GROUP) => ({
  release_group_mbid: RG, release_group: group, chosen, candidates: editions, policy: POLICY,
});

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

try {
  const { default: ReleaseChoice } = await server.ssrLoadModule("/src/components/ReleaseChoice.tsx");

  /** Render the panel once, with its answer already in the query cache (the
   *  component decides nothing itself — the payload IS the server's answer,
   *  keyed exactly as the component asks for it, override included). */
  const render = (override, answerFor, { primaryType = "", secondaryType = "" } = {}) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // The key the component asks with, the type props included.
    client.setQueryData(["mbReleaseChoice", RG, primaryType, secondaryType, override],
                        answerFor(override));
    return renderToString(
      React.createElement(QueryClientProvider, { client },
        React.createElement(ReleaseChoice, {
          releaseGroupMbid: RG,
          primaryType,
          secondaryType,
          override,
          onOverride: () => {},
        }))
    )
      // React separates adjacent text nodes with <!-- --> markers and escapes
      // quotes/apostrophes inside attribute values; neither is content, so the
      // assertions read the flattened text with both undone (every tooltip
      // survives, as its own plain sentence).
      .replace(/<!-- -->/g, "").replace(/&#x27;/g, "'").replace(/&#39;/g, "'")
      .replace(/&quot;/g, '"').replace(/&amp;/g, "&")
      .replace(/\s+/g, " ");
  };

  // A forced pick is what the server answers for `prefer=<mbid>`: that release,
  // with its own first reason replaced by the fact that the user asked for it.
  const forced = (prefer) => {
    const picked = EDITIONS.find((e) => e.release_mbid === prefer) ?? EDITIONS[0];
    return { ...picked, reasons: ["chosen because you asked for this release", ...picked.reasons.slice(1)] };
  };
  const ineligible = (e) => ({ ...e, eligible: false });

  const policyPick = render("", () => answer(EDITIONS[0], EDITIONS));
  const forcedPick = render(VINYL, (override) => answer(forced(override), EDITIONS));
  const refusedPick = render("", () => answer(EDITIONS[3], EDITIONS.map(ineligible)));
  const forcedRefusedPick = render(PROMO, (override) => answer(forced(override), EDITIONS.map(ineligible)));
  const wrongKind = render("", () => answer(null, EDITIONS.slice(0, 2).map(ineligible),
                                             { ...GROUP, primary_type: "Single" }),
                           { primaryType: "single" });
  const noEditions = render("", () => answer(null, []));

  const checks = [
    ["the policy's pick", policyPick, [
      ["says what it will fetch", "Will fetch"],
      ["the chosen edition's title", "Homework"],
      ["the chosen edition's facts, in one line", "1997-01-20 · FR · CD · 16 tracks · Official"],
      ["the medium reason as a chip", ">CD — preferred medium<"],
      ["the completeness reason as a chip", ">16/16 tracks<"],
      ["the status reason as a chip", ">official<"],
      ["the date reason as a chip", ">original edition<"],
      ["the medium chip's tooltip is the reason", "CD — preferred medium (order 1)"],
      ["...and the policy rule behind it", "Policy: the configured medium order decides first (CD, then other physical, then digital)"],
      ["the completeness chip carries its rule", "Policy: a release short of the release group's track count is penalised"],
      ["the status chip carries its rule", "Policy: official beats promotion beats bootleg; an unofficial release is only chosen when nothing official exists"],
      ["the date chip carries its rule", "Policy: the original edition beats a later reissue unless the later one is materially more complete"],
      ["how many editions were ranked", "4 editions ranked"],
      ["the ranked list stays closed while there is a fetchable pick", (flat) => !flat.includes("Vinyl — medium 3")],
    ]],
    ["a forced pick", forcedPick, [
      ["says the pick is the user's", "Will fetch (your pick)"],
      ["shows the forced edition's facts", "1997-01-20 · GB · Vinyl · 16 tracks · Official"],
      ["the forced reason is shown, not the policy's", ">chosen because you asked for this release<"],
      ["names the way back to the policy", "use the policy’s pick"],
      ["a refused-edition warning is NOT shown for an eligible pick", (flat) => !flat.includes("An unattended download will not take this edition")],
    ]],
    ["a pick auto-import refuses", refusedPick, [
      ["promises nothing", "Best edition — auto-import refuses it"],
      ["never claims it will be fetched", (flat) => !flat.includes("> Will fetch ")],
      ["quotes the rule that refuses it", "An unattended download will not take this edition — auto-import never takes a promotional or bootleg edition (auto_import_avoid_promo). Pick one below to fetch it anyway."],
      ["marks the pick itself not eligible", ">not eligible<"],
      ["the verdict tooltip names the rule", REFUSAL],
      ["the pick chip is honest about it", 'title="The policy\'s best edition, but an unattended download refuses it"'],
      ["opens the ranking, since choosing is now the user's job", "Vinyl — medium 3 in the configured order · 16/16 tracks of the release group"],
      ["still offers the override", ">Use this<"],
      ["does not offer to override the pick with itself", (flat) => !flat.includes("Fetch this edition instead — 1997-01-20 · US · CD · 1 track · Promotion")],
    ]],
    ["a forced pick the policy would refuse", forcedRefusedPick, [
      ["the user's own word wins over the refusal", "Will fetch (your pick)"],
      ["it is labelled as the user's pick, not the policy's", ">your pick<"],
      ["the pick chip says it was forced", 'title="The edition you forced"'],
      ["the verdict is still shown", ">not eligible<"],
      ["no refusal warning, because the add call uses the id as given", (flat) => !flat.includes("An unattended download will not take this edition")],
    ]],
    ["the kind asked for does not exist", wrongKind, [
      ["names the kind it looked for", "No Single edition in this release group — 2 other editions ranked."],
      ["still ranks what exists", "2 editions ranked"],
      ["opens the list, since the alternatives are the point", "Vinyl — medium 3 in the configured order · 16/16 tracks of the release group"],
      ["does not claim the group is empty", (flat) => !flat.includes("MusicBrainz knows no edition")],
    ]],
    ["no editions at all", noEditions, [
      ["says MusicBrainz holds none", "MusicBrainz knows no edition of this release group."],
      ["draws no list", (flat) => !flat.includes("editions ranked")],
    ]],
  ];

  for (const payloadPath of payloadPaths) {
    const body = JSON.parse(readFileSync(payloadPath, "utf8"));
    const chosen = body.chosen;
    const ranked = (body.candidates ?? []).slice(0, 20);
    const line = (e) => [e.date, e.country, (e.media ?? []).filter(Boolean).join(" + "),
                         e.track_count ? `${e.track_count} track${e.track_count === 1 ? "" : "s"}` : "",
                         e.status].filter(Boolean).join(" · ") || "no edition details";
    checks.push([`the server's own answer (${path.basename(payloadPath)})`, render("", () => body), [
      ["the pick is promised exactly when the rules allow it", (flat) =>
        flat.includes("Will fetch") === Boolean(chosen && chosen.eligible !== false)],
      ["the chosen edition and its facts", (flat) =>
        !chosen || (flat.includes(chosen.title) && flat.includes(line(chosen)))],
      ["every reason it gave is carried", (flat) =>
        !chosen || (chosen.reasons ?? []).every((r) => flat.includes(r))],
      ["a refused pick says so and quotes the rule", (flat) =>
        !chosen || chosen.eligible !== false ||
        (flat.includes("Best edition — auto-import refuses it") && flat.includes(chosen.reasons[0]))],
      ["what it ranked is counted or said to be none", (flat) =>
        ranked.length === 0
          ? flat.includes("MusicBrainz knows no edition")
          : flat.includes(`${ranked.length} edition`)],
      ["the ranking is drawn best first", (flat) => {
        // A row is the only thing that carries a score, so the scores are what
        // "best first" is measured on — the chosen block above the list shares
        // its text with the row for the same edition, which is no evidence.
        if (!chosen || chosen.eligible === false) {
          const places = ranked.map((e) => flat.indexOf(`Policy score ${(e.score ?? 0).toFixed(3)}`));
          return places.every((p) => p >= 0) && places.every((p, i) => i === 0 || places[i - 1] <= p);
        }
        return !flat.includes("Policy score");
      }],
    ]]);
  }

  const missing = [];
  let count = 0;
  for (const [state, html, wants] of checks) {
    for (const [what, want] of wants) {
      count += 1;
      const ok = typeof want === "function" ? want(html) : html.includes(want);
      if (!ok) missing.push(`${state}: ${what}`);
    }
  }

  /* ---- the artist page's release-group TYPE derivation --------------------
   *
   * `lib/artistReleaseGroups` is what the artist page's filter menu, its
   * collapsible sections and its per-type action rows all read, so the three
   * decisions the owner asked for are asserted here on the decisions
   * themselves — no DOM, no page, no server. The fixture is one discography
   * holding every shape the page has to survive: compound labels of both kind
   * (a secondary on a leading primary, and a long tail whose types tie on one
   * group each), which is exactly where a count-ordered bucket and a
   * label-ordered one part company. */
  const t = await server.ssrLoadModule("/src/lib/artistReleaseGroups.ts");
  const album = (n) => ({ title: `Studio ${n}`, primary_type: "Album", secondary_types: [] });
  const discography = [
    ...Array.from({ length: 5 }, (_, i) => album(i)),
    { title: "Live at the Apollo", primary_type: "Album", secondary_types: ["Live"] },
    { title: "Greatest Hits", primary_type: "Album", secondary_types: ["Compilation"] },
    { title: "EP One", primary_type: "EP", secondary_types: [] },
    { title: "EP Two", primary_type: "EP", secondary_types: [] },
    { title: "Hit", primary_type: "Single", secondary_types: [] },
    { title: "B-sides", primary_type: "Other", secondary_types: ["Compilation"] },
    { title: "More B-sides", primary_type: "Other", secondary_types: ["Compilation"] },
    { title: "Peel Session", primary_type: "Broadcast", secondary_types: [] },
    { title: "Untyped bootleg" },                      // no primary type at all
  ];
  const sections = t.typeSections(discography);
  const labels = sections.map((s) => s.label);
  const groupsOf = (label) => (sections.find((s) => s.label === label)?.groups ?? [])
    .map((g) => g.label);
  const options = [{ label: t.ALL_TYPES, count: discography.length, section: "", compound: false },
                   ...t.typeFilterOptions(sections)];
  const picked = (q) => t.filterTypeOptions(options, q).map((o) => o.label);
  const albumSection = sections.find((s) => s.label === "Album");
  const moreSection = sections.find((s) => s.label === "More");
  const liveOnly = t.selectedSections(sections, "Album + Live");
  const artWants = [
    ["Album, EP and Single lead, and nothing else does",
      JSON.stringify(labels) === JSON.stringify(["Album", "EP", "Single", "More"])
      && new Set(labels).size === labels.length],
    ["a leading section carries its own compound types, biggest first",
      JSON.stringify(groupsOf("Album"))
        === JSON.stringify(["Album", "Album + Compilation", "Album + Live"])],
    ["the long tail is one bucket, ordered by how many groups each type holds, "
      + "ties broken by label", JSON.stringify(groupsOf("More"))
        === JSON.stringify(["Other + Compilation", "Broadcast", "Other"])],
    ["the bucket's header counts every row in it", moreSection?.count === 4],
    ["no row is lost or counted twice",
      sections.reduce((n, s) => n + s.count, 0) === discography.length],
    ["the leading sections start open and the bucket starts folded",
      t.typeSectionOpen(albumSection, {}) === true && t.typeSectionOpen(moreSection, {}) === false],
    ["a press folds a leading section and unfolds the bucket",
      t.typeSectionOpen(albumSection, t.toggleTypeSection({}, albumSection)) === false
      && t.typeSectionOpen(moreSection, t.toggleTypeSection({}, moreSection)) === true],
    ["a type the reader named opens the bucket that holds it",
      t.typeSectionOpen(moreSection, {}, "Broadcast") === true],
    ["a discography of nothing but 'other' types opens its one section, having "
      + "no tail to fold",
      (() => {
        const only = t.typeSections([{ primary_type: "Other" }, { primary_type: "Broadcast" }]);
        return only.length === 1 && t.typeSectionOpen(only[0], {}) === true;
      })()],
    ["a compound pick is that one type, never every group of its primary",
      liveOnly.length === 1 && liveOnly[0].count === 1
      && JSON.stringify(liveOnly[0].groups.map((g) => g.label)) === JSON.stringify(["Album + Live"])],
    ["the menu offers only types the sections draw",
      options.slice(1).every((o) => labels.includes(o.label)
        || sections.some((s) => s.groups.some((g) => g.label === o.label)))],
    ["the menu's rows are the sections' rows, in one order",
      JSON.stringify(options.map((o) => o.label)) === JSON.stringify([
        "All", "Album", "Album + Compilation", "Album + Live", "EP", "Single",
        "More", "Other + Compilation", "Broadcast", "Other",
      ])],
    ["the filter finds a compound label by one of its words", picked("live").join() === "Album + Live"],
    ["...and by its whole spelling, spacing and case aside",
      picked("album+LIVE").join() === "Album + Live"],
    ["a section's own name is a pick of its every type", picked("Single").join() === "Single"],
    ["an empty box is every row, and a query nothing carries is none",
      picked("  ").length === options.length && picked("opera").length === 0],
    ["ranks are Album/EP/Single and then a tie for everything else",
      t.rgTypeRank("Album + Live") === 0 && t.rgTypeRank("EP + Compilation") === 1
      && t.rgTypeRank("Single") === 2 && t.rgTypeRank("Other") === 3
      && t.rgTypeRank("Broadcast") === 3],
    ["a group MusicBrainz served without a primary type is Other, and lands in the bucket",
      t.rgTypeLabel({ secondary_types: ["Demo"] }) === "Other + Demo"
      && groupsOf("More").includes("Other")],
  ];
  for (const [what, ok] of artWants) {
    count += 1;
    if (!ok) missing.push(`type derivation: ${what}`);
  }

  /* ---- the same decisions, in the ARTIST PAGE's own markup ----------------
   *
   * The pure decisions above say what the fold SHOULD be; only the page can
   * say whether a folded section really draws no rows. `MBArtistPage` is the
   * one App.tsx routes to, rendered here with its payload already in the query
   * cache and a router around it — no browser, but the real component, and the
   * markup is what a reader gets. */
  const { MBArtistPage, MBReleaseGroupPage } =
    await server.ssrLoadModule("/src/pages/MusicBrainzPage.tsx");
  // react-router-dom as the PAGE's own import resolves it (Node's "node"
  // condition -> dist/index.mjs), for the reason react-query is imported the
  // way it is above: two copies of it are two React contexts, and a
  // MemoryRouter from one would not be seen by the page's own useNavigate.
  const { MemoryRouter, Route, Routes } = await import(pathToFileURL(
    path.join(webDir, "node_modules/react-router-dom/dist/index.mjs")).href);
  const ARTIST = "a74b1b7f-71a5-4011-9441-d0b5e4122711";
  const rg = (id, title, primaryType, secondaryTypes = []) => ({
    id, title, primary_type: primaryType, secondary_types: secondaryTypes,
    first_release_date: "1997-01-20",
  });
  const artistGroups = [
    rg("a1", "Studio 1", "Album"), rg("a2", "Studio 2", "Album"), rg("a3", "Studio 3", "Album"),
    rg("a4", "Live at the Apollo", "Album", ["Live"]),
    rg("e1", "Come to Daddy", "EP"),
    rg("s1", "Windowlicker", "Single"),
    rg("o1", "Untyped bootleg", "Other"),
    rg("o2", "Rarities", "Other", ["Compilation"]),
    rg("b1", "Peel Session", "Broadcast"),
  ];
  const foldedPage = (() => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["mbArtist", ARTIST], {
      pages: [{ id: ARTIST, name: "Aphex Twin", release_groups: artistGroups,
                total: artistGroups.length, next: null }],
      pageParams: [0],
    });
    return renderToString(
      React.createElement(QueryClientProvider, { client },
        React.createElement(MemoryRouter, { initialEntries: [`/mb/artist/${ARTIST}`] },
          React.createElement(Routes, null,
            React.createElement(Route, {
              path: "/mb/artist/:id",
              element: React.createElement(MBArtistPage),
            })))))
      .replace(/<!-- -->/g, "").replace(/&#x27;/g, "'").replace(/&#39;/g, "'")
      .replace(/&quot;/g, '"').replace(/&amp;/g, "&")
      .replace(/\s+/g, " ");
  })();
  const at = (needle) => foldedPage.indexOf(needle);
  // A reader's view of one fragment: the page's own words with the tags taken
  // out — a count sits in its own element, so text alone would run the label
  // and the number together with markup in between.
  const asRead = (from, span) =>
    (from < 0 ? "" : foldedPage.slice(from, from + span))
      .replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  // The section headers' own words: a folded header a reader can press says
  // "Show … (n)", an open one says "Fold … away". Nothing else on the page
  // carries either sentence, so these are the folds themselves.
  const albumHead = "Fold this artist's Album release groups away";
  const epHead = "Fold this artist's EP release groups away";
  const singleHead = "Fold this artist's Single release groups away";
  const moreHead = "Show this artist's More release groups (3)";
  const pageWants = [
    ["the four sections are drawn in the derivation's order",
      [albumHead, epHead, singleHead, moreHead].every((h) => at(h) >= 0)
      && at(albumHead) < at(epHead) && at(epHead) < at(singleHead)
      && at(singleHead) < at(moreHead)],
    ["Album, EP and Single are OPEN and the bucket is FOLDED, in the markup",
      (foldedPage.match(/Fold this artist's/g) ?? []).length === 3 && at(moreHead) >= 0],
    ["an open section draws its rows, its compound types named",
      at("Studio 1") >= 0 && at("Studio 2") >= 0 && at("Studio 3") >= 0
      && at("Live at the Apollo") >= 0 && at("Album + Live · 1") >= 0
      && at("Come to Daddy") >= 0 && at("Windowlicker") >= 0],
    ["the folded bucket draws NONE of its rows",
      ["Peel Session", "Rarities", "Untyped bootleg"].every((t) => at(t) === -1)],
    ["...but its types are still one press away in the add panel",
      at("Broadcast") >= 0 && at("Other + Compilation") >= 0],
    ["the filter's trigger says what is on screen and how many rows that is",
      /^All types \(9\)/.test(asRead(at("All types"), 160))],
  ];
  for (const [what, ok] of pageWants) {
    count += 1;
    if (!ok) missing.push(`artist page: ${what}`);
  }

  /* ---- the release-group page's editions table ---------------------------
   *
   * The owner asked for each edition's CATALOG NUMBER in a column of its own,
   * immediately before the bar code. The number is already IN the payload the
   * page holds — `release_group_browse` reads every release's own
   * `label-info` through `mlo.release_choice.catalog_numbers` (the reader the
   * release choice ranks by), so `catalog_numbers` / `catalog_number` ride each
   * row — and what is asserted here is the table: the header's own order, the
   * row's own numbers joined the app's way, the tooltip that carries a value
   * an ellipsis may cut, and the same em dash beside an edition that states
   * none that the Date column prints. */
  const RGV = "aaaa0000-1111-2222-3333-444455556666";
  const edition = (id, title, catNumbers, barcode) => ({
    id, title, alias: null, date: "1997-01-20", country: "JP", status: "Official",
    disambiguation: "", medium: "CD", formats: "1×CD", disc_count: 1, track_count: 16,
    track_breakdown: "", barcode, catalog_numbers: catNumbers,
    catalog_number: catNumbers[0] ?? "", score: 1, reasons: [],
  });
  const rgPayload = {
    id: RGV, title: "Homework", alias: null, disambiguation: "",
    artist: "Daft Punk", artist_mbid: null, primary_type: "Album", secondary_types: [],
    genres: [], first_release_date: "1997-01-20", podcast: null,
    countries: [], total: 2, offset: 0, next: null,
    releases: [
      edition("rel-cat", "Homework (Japanese edition)", ["SRCS 8757", "CK 62240"], "4988009875798"),
      edition("rel-nocat", "Homework (promo)", [], ""),
    ],
  };
  const rgPage = (() => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["mbRG", RGV], { pages: [rgPayload], pageParams: [0] });
    return renderToString(
      React.createElement(QueryClientProvider, { client },
        React.createElement(MemoryRouter, { initialEntries: [`/mb/rg/${RGV}`] },
          React.createElement(Routes, null,
            React.createElement(Route, {
              path: "/mb/rg/:id",
              element: React.createElement(MBReleaseGroupPage),
            })))))
      .replace(/<!-- -->/g, "").replace(/&#x27;/g, "'").replace(/&#39;/g, "'")
      .replace(/&quot;/g, '"').replace(/&amp;/g, "&")
      .replace(/\s+/g, " ");
  })();
  // The header row's own words, tags aside: the columns in the order they are
  // drawn, so "Cat #" before "Barcode" is adjacency and not just both present.
  const headText = (() => {
    const from = rgPage.indexOf("<thead");
    const to = rgPage.indexOf("</thead>");
    return from < 0 || to < 0 ? ""
      : rgPage.slice(from, to).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  })();
  const catCell = "SRCS 8757 + CK 62240";
  // The cell's own classes, so the count is of Cat # cells and not of every
  // `cell-ellipsis` in the table (the Tracks column carries one too).
  const catCellClass = /font-mono text-\[11px\] cell-ellipsis/;
  const rgWants = [
    ["the editions table's columns are Date, Title, Format, Discs, Tracks, Country, "
      + "Cat #, Barcode",
      headText === "Date Title Format Discs Tracks Country Cat # Barcode"],
    ["the Cat # column carries a pixel floor, like its neighbours",
      /class="th w-\[150px\]">Cat #<\/th>/.test(rgPage)],
    ["a row prints its OWN catalog numbers, joined the app's way",
      rgPage.includes(catCell)],
    ["a value an ellipsis may cut is readable whole in the tooltip",
      rgPage.includes(`title="${catCell}"`)],
    ["every edition draws one Cat # cell",
      (rgPage.match(new RegExp(catCellClass.source, "g")) ?? []).length === 2],
    ["an edition that states no number says so with the Date column's em dash",
      (rgPage.match(new RegExp(`${catCellClass.source}"(?: title="[^"]*")?>—</td>`)) ?? []).length === 1],
  ];
  for (const [what, ok] of rgWants) {
    count += 1;
    if (!ok) missing.push(`release-group page: ${what}`);
  }
  if (missing.length) {
    console.error("[choice] MISSING: " + JSON.stringify(missing));
    console.error(refusedPick.replace(/></g, ">\n<").split("\n")
      .filter((l) => /chip|button|<li/.test(l)).slice(0, 25).join("\n"));
    process.exit(1);
  }
  console.log("ok  the release-choice panel says which edition it will fetch, why, " +
    `and which ones it ranked (${count} checks over ${checks.length} states` +
    `${payloadPaths.length ? `, including ${payloadPaths.map((p) => path.basename(p)).join(", ")}` : ""}), ` +
    `and the artist page's type derivation leads with Album/EP/Single, folds the ` +
    `long tail into one count-ordered bucket and finds a compound type by name ` +
    `(${artWants.length} decisions), with the folds the page really draws ` +
    `(${pageWants.length} in its markup), and the release-group page's editions ` +
    `table carries each edition's own catalog number beside its bar code ` +
    `(${rgWants.length} in its markup)`);
} finally {
  await server.close();
}
