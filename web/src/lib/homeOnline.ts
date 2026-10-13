import { api } from "../api";
import { SHELF_LIMIT } from "../components/RecommendShelf";

/** The three online shelves Home draws below its library shelves — their own
 *  requests, cache keys and cache windows, kept out of HomePage.tsx so the
 *  page module exports components and nothing else.
 *
 *  One key and one window per shelf: a provider behind one can never make the
 *  others refetch or wait, and a page that loses one still shows the rest.
 *
 *  Exported so tools/check_home_online.mjs drives the SAME functions the page
 *  does: the Apple-then-Deezer fallback below is a rule about the REQUEST, and
 *  a check that re-wrote it would prove nothing about the page.
 *
 *  * trending — Apple Music's most-played songs feed (keyless, the chart Apple
 *    refreshes daily, which is why it answers the `all` window). Deezer's own
 *    chart is its stand-in ONLY when Apple ANSWERS with nothing: a provider
 *    that is down raises, and its shelf says nothing rather than quietly
 *    becoming Deezer's chart.
 *  * fresh — no provider wired into the backend publishes a dated
 *    new-releases feed, so this is the closest it serves for albums: Deezer's
 *    current album chart (keyless). The shelf's own line says what it is
 *    rather than claiming a release date the answer does not carry.
 *  * forYou — seeded by THIS library, its genre mix and its most-collected
 *    artists (`seed=library`); everything the library already owns is left
 *    out, because a recommendation is something to add. */
export const HOME_ONLINE = {
  trending: {
    queryKey: ["homeOnline", "trending"] as const,
    staleTime: 10 * 60_000,
    queryFn: async () => {
      const apple = await api.discoverCharts({ period: "all", kind: "tracks", source: "itunes", limit: SHELF_LIMIT });
      if (apple.items?.length) return apple;
      return api.discoverCharts({ period: "all", kind: "tracks", source: "deezer", limit: SHELF_LIMIT });
    },
  },
  fresh: {
    queryKey: ["homeOnline", "new"] as const,
    staleTime: 10 * 60_000,
    queryFn: () => api.discoverCharts({ period: "all", kind: "albums", source: "deezer", limit: SHELF_LIMIT }),
  },
  forYou: {
    queryKey: ["homeOnline", "forYou"] as const,
    staleTime: 15 * 60_000,
    queryFn: () => api.discoverRecommended({ seed: "library", kind: "albums", limit: SHELF_LIMIT }),
  },
};