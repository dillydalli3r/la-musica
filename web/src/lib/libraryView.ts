/** The Library's browse state: which view the album grid is in, the cover
 *  size its grid draws at, what each table is sorted by, and the option lists
 *  the toolbar is built from.
 *
 *  Here rather than in the page because the Library is no longer the only page
 *  that offers them — Home's shelves draw the same cards and share the same
 *  cover size — and two copies of a control's state is how two pages end up
 *  disagreeing about what the user picked.
 *
 *  What is NOT here is the STATE of the table-only controls — which preset is
 *  picked, the group-by-artist toggle, the column prefs — because only the
 *  page that draws that table offers them and a "shared" module holding state
 *  with a single reader would be indirection. Their option LISTS live here
 *  with the rest, so a page that later offers one of those controls reads the
 *  same list of choices.
 */

import { useState } from "react";
import { useStore } from "../store";
import type { SortState } from "./sort.tsx";

export type LibraryView = "grid" | "compact" | "albums" | "artists" | "tracks";

export const VIEW_TABS: { id: LibraryView; label: string }[] = [
  { id: "grid", label: "Grid" },
  { id: "compact", label: "Compact" },
  { id: "albums", label: "Albums" },
  { id: "artists", label: "Artists" },
  { id: "tracks", label: "Tracks" },
];

/** Cover size in the grid view — the same segmented control as the view tabs. */
export type GridSize = "s" | "m" | "l";

export const GRID_SIZES: { id: GridSize; label: string }[] = [
  { id: "s", label: "S" },
  { id: "m", label: "M" },
  { id: "l", label: "L" },
];

/** Quick-filter presets — the album-table conditions worth one click.
 *
 *  Explicit is NOT here any more: the advisory is a three-state ladder (0 not
 *  explicit / 1 explicit / 2 clean edition, see `AdvisoryBadge`), so it needs a
 *  facet of its own rather than one preset that could only ever mean "1". Two
 *  controls for one condition is how they end up disagreeing. */
export type Preset =
  | "all"
  | "failing"
  | "cd"
  | "digital"
  | "instrumental"
  | "missingLyrics"
  | "videos";

export const PRESETS: { id: Preset; label: string }[] = [
  { id: "all", label: "All" },
  { id: "failing", label: "Failing" },
  { id: "cd", label: "CD rips" },
  { id: "digital", label: "Digital" },
  { id: "instrumental", label: "Instrumental" },
  { id: "videos", label: "Music videos" },
  { id: "missingLyrics", label: "No lyrics" },
];

/** Star-rating facet — "have I rated this yet", which is a question about the
 *  user's own verdicts and nothing else. See `RATED_NOTE`. */
export type RatingFilter = "any" | "rated" | "unrated";

export const RATING_FILTERS: { id: RatingFilter; label: string; hint: string }[] = [
  { id: "any", label: "Any rating", hint: "Ignore the stars — show everything" },
  { id: "rated", label: "Rated", hint: "Only rows you have given a star rating" },
  { id: "unrated", label: "Unrated", hint: "Only rows you have not rated yet" },
];

/** What "rated" means for a row that has no rating of its own. An album's
 *  stars are the FOLDER rating (its own verdict, stored in the DB); a track's
 *  are its file's. Read as one sentence everywhere the facet is applied, so
 *  the albums, artists and tracks tables cannot describe it differently — and
 *  an album is only RATED once the verdict on the album itself is in AND every
 *  track of it carries one of its own: a half-rated album is not finished, and
 *  naming it in the "Unrated" list is exactly what that list is for. */
export const RATED_NOTE =
  "An album counts as rated when its own folder rating is set AND every track in it is rated; an artist when any of its albums is. Nothing here is an average.";

/** Advisory facet — the app's own three-state ladder, in the two questions a
 *  listener actually asks: "show me the explicit ones" and "keep them away
 *  from me". Clean therefore means every advisory that is not 1: 2 (the clean
 *  EDITION the badges draw) and 0/absent (nothing marked it explicit). */
export type AdvisoryFilter = "any" | "explicit" | "clean";

export const ADVISORY_FILTERS: { id: AdvisoryFilter; label: string; hint: string }[] = [
  { id: "any", label: "Any advisory", hint: "Ignore ITUNESADVISORY — show everything" },
  { id: "explicit", label: "Explicit", hint: "ITUNESADVISORY 1 — a track or album that flags explicit content" },
  {
    id: "clean",
    label: "Clean",
    hint: "Nothing flags explicit: ITUNESADVISORY 0/absent (not explicit) or 2 (clean edition)",
  },
];

export const ALBUM_SORTS: { key: string; label: string }[] = [
  { key: "meta.ALBUM", label: "Album name" },
  { key: "artist", label: "Artist" },
  { key: "meta.DATE", label: "Year" },
  { key: "track_count", label: "Tracks" },
  { key: "grade_pct", label: "Grade" },
  { key: "audit_summary", label: "Audit" },
  { key: "video_count", label: "Music videos" },
  { key: "inst_count", label: "Instrumental tracks" },
  { key: "meta.LABEL", label: "Label" },
  { key: "meta.CATALOGNUMBER", label: "Catalog #" },
];

/** The view a fresh visit opens in — written by Settings ("Default view") and
 *  read here. */
const VIEW_KEY = "mlo.defaultView.v2";

/** Cover size, read by every grid that draws album covers. */
const GRID_SIZE_KEY = "mlo.gridSize";

/** The stored browse view. Read once and NOT written back while browsing:
 *  the same key is Settings' default view, and a tab click that saved itself
 *  would make every later visit start in whatever was last clicked.
 *
 *  Membership is CHECKED, not cast: the key outlives the option list (a stale
 *  id, or the empty string a cleared field leaves behind), and an id that is
 *  none of the five matches none of the page's `view === …` branches — a
 *  page that renders no view at all. An id we no longer offer falls back to
 *  the grid, exactly like a missing one. */
export function useLibraryView(): [LibraryView, (v: LibraryView) => void] {
  const [view, setView] = useState<LibraryView>(() => {
    const stored = localStorage.getItem(VIEW_KEY);
    return VIEW_TABS.some((t) => t.id === stored) ? (stored as LibraryView) : "grid";
  });
  return [view, setView];
}

/** The cover size the grids draw at, persisted: picking L on Home is the size
 *  the Library opens in, and the other way round. */
export function useGridSize(): [GridSize, (s: GridSize) => void] {
  const [size, setSize] = useState<GridSize>(() => {
    const v = localStorage.getItem(GRID_SIZE_KEY);
    return v === "s" || v === "l" ? v : "m";
  });
  const pick = (s: GridSize) => {
    setSize(s);
    try {
      localStorage.setItem(GRID_SIZE_KEY, s);
    } catch {
      /* ignore */
    }
  };
  return [size, pick];
}

/** Select mode: the checkbox pass over the grid cards. The SELECTION itself is
 *  the app's global store (`store.ts` — every batch action in the app reads
 *  it), so a page only owns the on/off that draws the checkboxes. */
export function useSelectMode(): { selectMode: boolean; toggleSelectMode: () => void } {
  const clearSelection = useStore((s) => s.clearSelection);
  const [selectMode, setSelectMode] = useState(false);
  // Leaving select mode drops what was ticked: the batch toolbar reads the
  // store, so a selection left behind a hidden checkbox would be acted on
  // while nothing on screen shows it as chosen.
  const toggleSelectMode = () =>
    setSelectMode((on) => {
      if (on) clearSelection();
      return !on;
    });
  return { selectMode, toggleSelectMode };
}

/** Sort state per table view, persisted like the column prefs (each view —
 * albums / artists / tracks — keeps its own key, so switching tabs or
 * reloading no longer resets the other tables). */
export function useLocalSort(key: string): [SortState | null, (key: string) => void] {
  const storageKey = `mlo-sort-${key}`;
  const [sort, setSort] = useState<SortState | null>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw) as SortState;
        if (parsed && typeof parsed.key === "string" && (parsed.dir === 1 || parsed.dir === -1)) return parsed;
      }
    } catch {
      /* fall through to unsorted */
    }
    return null;
  });
  const set = (k: string) => {
    setSort((cur) => {
      const dir: 1 | -1 = cur && cur.key === k && cur.dir === 1 ? -1 : 1;
      const next: SortState = { key: k, dir };
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };
  return [sort, set];
}
