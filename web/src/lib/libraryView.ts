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
  | "videos"
  | "podcasts";

export const PRESETS: { id: Preset; label: string }[] = [
  { id: "all", label: "All" },
  { id: "failing", label: "Failing" },
  { id: "cd", label: "CD rips" },
  { id: "digital", label: "Digital" },
  { id: "instrumental", label: "Instrumental" },
  { id: "videos", label: "Music videos" },
  // Not a medium and not a MusicBrainz release-group type: a podcast episode
  // is a release group linked `part of` a series of type Podcast (see
  // mlo.naming.DERIVED_RELEASE_TYPES), and the app records that series on the
  // episode's own files — so this preset asks the album's `podcast` block,
  // which a scan fills without asking MusicBrainz.
  { id: "podcasts", label: "Podcasts" },
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

/* ---- the alphabet filter: the toolbar's name field and its A–Z rail -------
 *
 *  Two controls over ONE pair of values (the typed words and the picked
 *  letter), so they live here with the rest of the browse state rather than in
 *  the page's own head: whichever of the two the reader uses, the list is cut
 *  by the one predicate below, and a later page that offers the same pair gets
 *  the same answer instead of a second opinion about what "B" means.
 *
 *  NOT persisted and never in the URL: this is a pass over the list in front of
 *  you ("where is that album again"), not a place the page can be linked back
 *  to. A fresh visit opens on the whole library, and a reload is a fresh visit.
 */

/** The rail's letters, in the order it draws them: A–Z and then `#`, which
 *  holds everything with no A–Z initial of its own — a digit ("1989"), a
 *  symbol ("…And Justice for All"), a script this rail has no letter for. */
export const AZ_LETTERS: readonly string[] = [
  "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
  "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "#",
];

/** Fold a display name down to what the alphabet controls compare: lower case
 *  with the accents taken off (NFKD, then the combining marks it left behind).
 *  "Ásgeir" is therefore found by "asgeir" — a name box that demanded the
 *  accent would be a box most keyboards cannot answer. */
export function foldName(name: string): string {
  return name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

/** The rail's letter for an ALREADY folded name — the inner half of
 *  `azLetterOf`, for callers (the menu's counts) that fold a row once and then
 *  ask it both questions. */
export function letterOfFolded(folded: string): string {
  const first = folded.charAt(0);
  return first >= "a" && first <= "z" ? first.toUpperCase() : "#";
}

/** The rail's letter for a display name: its own FIRST character when that is
 *  one of the 26, `#` otherwise. Read from the folded name, so "Émile" is E.
 *
 *  The first character, not the first letter anywhere in it: an index that
 *  files "The Beatles" under B has guessed which word is the name, and a click
 *  on T answered with nothing would be the list arguing with its own rail. */
export function azLetterOf(name: string): string {
  return letterOfFolded(foldName(name.trim()));
}

/** The one predicate both alphabet controls make. `needle` is the field's
 *  words ALREADY folded with `foldName` (the caller folds them once per list,
 *  not once per row); `letter` is the rail's pick, or null for all of them.
 *  The two COMPOSE — a row has to satisfy both — and a row nobody is looking
 *  for is every row. */
export function azKeep(name: string, needle: string, letter: string | null): boolean {
  const folded = foldName(name.trim());
  if (letter && letterOfFolded(folded) !== letter) return false;
  return !needle || folded.includes(needle);
}

/** The rows of one list the alphabet controls keep, in the order they arrived:
 *  the sort above decides the order, this only removes rows — which is why it
 *  returns the same array when neither control is set. */
export function azFilter<T>(
  rows: T[],
  nameOf: (row: T) => string,
  needle: string,
  letter: string | null
): T[] {
  if (!needle && !letter) return rows;
  return rows.filter((row) => azKeep(nameOf(row), needle, letter));
}

/** How many of `names` sit under each rail letter — the count the menu prints
 *  beside a letter, taken over the rows the rest of the page's controls (and
 *  the name field) would leave, but never over the letter itself. That is what
 *  makes the number worth printing: it says what a click would leave. */
export function azCounts(names: readonly string[], needle: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const name of names) {
    if (!azKeep(name, needle, null)) continue;
    const letter = azLetterOf(name);
    out[letter] = (out[letter] ?? 0) + 1;
  }
  return out;
}

/** The alphabet toolbar's own state. One hook for both controls because they
 *  are one filter (see `azKeep`), and `letter: null` is the rail switched off
 *  — not "the `#` letter", which is a bucket of its own. */
export function useLibraryAlphabet(): {
  name: string;
  setName: (v: string) => void;
  letter: string | null;
  setLetter: (v: string | null) => void;
} {
  const [name, setName] = useState("");
  const [letter, setLetter] = useState<string | null>(null);
  return { name, setName, letter, setLetter };
}

/** Sort state per table view, persisted like the column prefs (each view —
 *  albums / artists / tracks — keeps its own key, so switching tabs or
 *  reloading no longer resets the other tables). */
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
