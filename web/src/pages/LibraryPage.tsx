import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowDownUp, BarChart3, ChevronDown, ChevronRight, CloudDownload,
  FileVideo, FolderSync, FolderTree, Info as InfoIcon, Layers, Library, ListChecks,
  ListFilter, ListPlus, Play, RefreshCw, Search, Tag, Trash2, Wand2, X,
} from "lucide-react";
import { api } from "../api";
import { SCRIPTS, DEFAULT_RUN_ALL, isScriptId } from "../lib/scripts";
import { toast, useStore } from "../store";
import {
  sortRows, SortHeader, groupByDisc, byDiscThenTrack,
} from "../lib/sort.tsx";
import {
  ColumnResizer, ColumnsMenu, useColumnPrefs, useColumnWidths, useCustomColumns,
  customColValue, customCols, ALBUM_TRACK_COLS, ALBUM_TRACK_COL_W, ALBUM_TRACK_MIN_W, TABLE_FIT, TAG_COL_W,
  // The track table's own columns, floors and phone folds — shared with the
  // Export page's preview so the two tables cannot drift apart.
  TRACK_COLS, TRACK_COL_W, TRACK_PHONE_CLS, TRACK_RATING_COL, PHONE_HIDE, phoneHide,
  type Col, type CustomCol,
} from "../lib/columns";
import { gradeSliver, statusFor, auditFails } from "../lib/status";
import { invalidateLibrary } from "../lib/invalidate";
import { albumRef, trackRef, artistRef, entityLinkClick } from "../lib/refs";
import { fmtTech, fmtDuration, fmtDateCell, originalYear, GRID_SIZE_MIN } from "../lib/fmt";
import { EmptyState, GradeBadge, MediaChip, AdvisoryMark, CachedMark, PageLoading, PendingMark } from "../components/Badges";
import LockedChip from "../components/LockedChip";
import { forceDict, loadForceSel } from "../lib/force";
import Segmented from "../components/Segmented";
import PageHeader from "../components/PageHeader";
import StarRating from "../components/StarRating";
import { ratingOf, useRatings, useSetRating, FOLDER_RATING_NOTE } from "../lib/ratings";
import CoverImg, { TrackCover } from "../components/CoverImg";
import FavHeart from "../components/FavHeart";
import TrackTitleCell from "../components/TrackTitleCell";
import AlbumCard from "../components/AlbumCard";
import { useAcquisitions, type Acquisition } from "../lib/acquisition";
import AlbumRow, { type AlbumRowCell } from "../components/AlbumRow";
import StatsPanel from "../components/StatsPanel";
import TrackDetails from "../components/TrackDetails";
import BulkTagsDialog from "../components/BulkTagsDialog";
import { TrackActionsMenu } from "../components/TagActionsMenu";
import type { Album, Artist, Track } from "../types";

// The Library's browse state and option lists live in lib/libraryView.ts —
// Home's shelves offer the same cover size and read the same settings.
import {
  ALBUM_SORTS, GRID_SIZES, PRESETS, RATING_FILTERS, ADVISORY_FILTERS, RATED_NOTE, VIEW_TABS,
  useGridSize, useLibraryView, useLocalSort, useSelectMode,
  type Preset, type RatingFilter, type AdvisoryFilter,
} from "../lib/libraryView";

/** One floor per column, in px: the narrowest that column can be before its
 *  content starts wrapping a character per line. They also are the table's
 *  floor, summed by TABLE_FIT's `min-w-max`, so a window wider than their sum
 *  shares the extra out in proportion — the old percentages took their cut of
 *  whatever width the table had, which is how the album name column in this
 *  very table ended up at 0 px even on a 1440 px screen. */
const ALBUM_COL_W: Record<string, string> = {
  // `md:` because below that the phone fold has already dropped the columns
  // beside it, and the name shares the row with the cover, the chevron and the
  // row actions — a 220 px floor there would push those off the screen.
  album: "md:w-[220px]",
  artist: "w-[116px]",
  year: "w-16",
  // 72, not 64: the header's own label ("TRACKS" at 11 px, tracked out) plus
  // the sort arrow is 67 px wide, and a nowrap header wider than its column
  // paints over the neighbour in a fixed-layout table.
  tracks: "w-[72px]",
  // Five `sm` stars (14 px each) plus the hover room a click target needs.
  rating: "w-[104px]",
  grade: "w-20",
  // "Digital Media" is the MediumChip's own longest label: 108 px, measured —
  // the old 88 broke the chip across two lines.
  media: "w-[112px]",
  dr: "w-12",
  source: "w-20",
  videos: "w-20",
  inst: "w-20",
};

const ALBUM_COLS: Col[] = [
  { id: "album", label: "Album", sortKey: "meta.ALBUM" },
  { id: "artist", label: "Artist", sortKey: "artist" },
  { id: "year", label: "Year", sortKey: "meta.DATE" },
  { id: "tracks", label: "Tracks", sortKey: "track_count" },
  // The album's OWN rating (the store's album scope), which the caller adds to
  // the row as `rating` — see `ratedAlbums` in LibraryPage.
  { id: "rating", label: "Rating", sortKey: "rating" },
  { id: "grade", label: "Grade", sortKey: "grade_pct" },
  { id: "media", label: "Media", sortKey: "media" },
  // ADR, not DR: the column sorts and shows the ALBUM's own dynamic range
  // (the release's one value), while the track tables' `dr` columns show each
  // track's DYNAMIC RANGE. Same letters, different tag - and the album page
  // has called it ADR since it drew the chip.
  { id: "dr", label: "ADR", sortKey: "meta.ALBUM DYNAMIC RANGE" },
  { id: "source", label: "Source", sortKey: "source_summary" },
  { id: "videos", label: "Videos", sortKey: "video_count" },
  { id: "inst", label: "INST", sortKey: "inst_count" },
];

/** Same floors as the album table for the shared column ids; the artist table's
 *  own name column is sized where it renders (see the Artist header th). */
const ARTIST_COL_W: Record<string, string> = {
  albums: "w-[88px]",
  tracks: "w-[88px]",
  checks: "w-[88px]",
  grade: "w-[112px]",
};

const ARTIST_COLS: Col[] = [
  { id: "albums", label: "Albums", sortKey: "aggregate.album_count" },
  { id: "tracks", label: "Tracks", sortKey: "aggregate.track_count" },
  // The pass COUNT, not the grade percentage: the cell reads
  // "pass_count/total_checks", and sharing Grade's key lit both headers at
  // once and made a click on Checks sort by the grade it does not show.
  { id: "checks", label: "Checks", sortKey: "aggregate.pass_count" },
  { id: "grade", label: "Grade", sortKey: "aggregate.grade_pct" },
];

/** Which columns the album table folds on a phone (the shared `PHONE_HIDE`
 *  class). The two TRACK tables' fold map is `TRACK_PHONE_CLS` in
 *  lib/columns — shared with the Export page's preview. */
const ALBUM_PHONE_CLS: Record<string, string> = {
  artist: PHONE_HIDE, year: PHONE_HIDE, tracks: PHONE_HIDE, rating: PHONE_HIDE,
  grade: PHONE_HIDE, media: PHONE_HIDE, dr: PHONE_HIDE, source: PHONE_HIDE,
  videos: PHONE_HIDE, inst: PHONE_HIDE,
};
/** The artist table keeps its grade badge and drops the aggregate counters. */
const ARTIST_PHONE_CLS: Record<string, string> = {
  albums: PHONE_HIDE, tracks: PHONE_HIDE, checks: PHONE_HIDE,
};
interface FlatAlbum extends Album {
  artist: string;
  video_count: number;
  inst_count: number;
  hay: string; // lowercase search blob, built once per payload
}

interface FlatTrack extends Track {
  artist: string;
  album: string;
  albumCover?: string | null;
  albumPath: string;
  hay: string; // lowercase search blob, built once per payload
}

// ---- search: plain words + tag-scoped terms -------------------------------
/** Tags matched by the "person:" alias — everyone credited on the song. */
const PERSON_TAG_KEYS = ["ARTIST", "ALBUMARTIST", "COMPOSER", "LYRICIST", "REMIXER"];

/** Friendly `key:value` aliases for tag-scoped search. "#person" matches any
 *  credited person tag, "#any" matches every tag. Unknown keys fall back to
 *  plain-word matching. */
const TAG_QUERY_ALIASES: Record<string, string> = {
  title: "TITLE", track: "TITLE", artist: "ARTIST", albumartist: "ALBUMARTIST",
  album: "ALBUM", genre: "GENRE", year: "DATE", date: "DATE",
  media: "MEDIA", source: "SOURCE", label: "LABEL",
  catalog: "CATALOGNUMBER", catalogue: "CATALOGNUMBER",
  country: "RELEASECOUNTRY", releasetype: "RELEASETYPE", type: "RELEASETYPE",
  isrc: "ISRC", copyright: "COPYRIGHT",
  composer: "COMPOSER", lyricist: "LYRICIST", remixer: "REMIXER",
  person: "#person", people: "#person", involved: "#person",
  tag: "#any", any: "#any",
};

interface QueryTerms { words: string[]; tags: { key: string; value: string }[] }

/** Split the search box into plain words and tag-scoped terms. Quoted values
 *  keep spaces: composer:"Hans Zimmer". */
function parseQueryTerms(raw: string): QueryTerms {
  const words: string[] = [];
  const tags: { key: string; value: string }[] = [];
  const tokens = raw.match(/\S+:"[^"]*"|"[^"]*"|\S+/g) ?? [];
  for (const tok0 of tokens) {
    const tok = tok0.replace(/^"|"$/g, "");
    if (!tok) continue;
    const m = /^([^:\s]+):(.+)$/.exec(tok);
    const alias = m ? TAG_QUERY_ALIASES[m[1].toLowerCase()] : undefined;
    if (m && alias) {
      const value = m[2].replace(/^"|"$/g, "").toLowerCase();
      if (value) tags.push({ key: alias, value });
    } else {
      words.push(tok.toLowerCase());
    }
  }
  return { words, tags };
}

export default function LibraryPage() {
  // Refresh means "look at the music folder AGAIN", not "ask again": the
  // server caches this payload (tagcache's library entry) and every album's
  // tech/grade reads under it, so a plain refetch redrew the same tree — the
  // same trap `/api/home?refresh=1` was written for, which is why both buttons
  // now go through one server-side drop (`main._refresh_library_caches`). The
  // flag is one-shot and a ref rather than a query key, so an ordinary
  // background refetch (a remount, a save's invalidation) keeps using the
  // cache instead of forcing a re-walk every time.
  const forceRefresh = useRef(false);
  const { data: lib, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["library"],
    queryFn: () => {
      const force = forceRefresh.current;
      forceRefresh.current = false;
      return api.library(force);
    },
  });
  const refresh = () => {
    forceRefresh.current = true;
    void refetch();
  };
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  // The layout report the last scan stored (script 20, or the Optimization
  // panel's Scan). Read, never walked: this is what lets the page state a
  // library-wide condition on every visit without scanning the library for
  // it — and `exists: false` is what keeps a warning off the screen until a
  // scan has actually run.
  const { data: layout } = useQuery({ queryKey: ["layout-report"], queryFn: api.libraryLayoutReport });
  const runAllIds = Array.isArray(config?.run_all_order) && config.run_all_order.length
    ? config.run_all_order.filter((n: number) => isScriptId(n))
    : DEFAULT_RUN_ALL;
  const qc = useQueryClient();
  const navigate = useNavigate();
  const query = useStore((s) => s.query);
  // The Library's own search box edits the SAME store value the top bar does:
  // one filter, so the two boxes can never disagree about what is being shown.
  const setQuery = useStore((s) => s.setQuery);
  // Debounced 200ms: the filter memo only recomputes after typing pauses.
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 200);
    return () => clearTimeout(t);
  }, [query]);
  const selection = useStore((s) => s.selection);
  const setSelection = useStore((s) => s.setSelection);
  const toggleTrack = useStore((s) => s.toggleTrack);
  const toggleAlbum = useStore((s) => s.toggleAlbum);
  const toggleArtist = useStore((s) => s.toggleArtist);
  const clearSelection = useStore((s) => s.clearSelection);
  const playNow = useStore((s) => s.playNow);
  const [view, setView] = useLibraryView();
  // checkboxes (and the batch toolbar they feed) only exist in select mode
  const { selectMode, toggleSelectMode } = useSelectMode();
  const [preset, setPreset] = useState<Preset>("all");
  // The two facets beside the presets: the user's OWN star ratings ("what have
  // I not rated yet") and the advisory ladder. Both are facets rather than
  // presets because each has more than one answer worth picking — the preset
  // list is conditions you either want or do not.
  const [ratingFilter, setRatingFilter] = useState<RatingFilter>("any");
  const [advisoryFilter, setAdvisoryFilter] = useState<AdvisoryFilter>("any");
  const [filterOpen, setFilterOpen] = useState(false);
  const [sortOpen, setSortOpen] = useState(false);
  const [albumSort, setAlbumSort] = useLocalSort("albums");
  const [artistSort, setArtistSort] = useLocalSort("artists");
  const [trackSort, setTrackSort] = useLocalSort("tracks");
  const [removing, setRemoving] = useState<string | null>(null);
  const [lyricsBusy, setLyricsBusy] = useState(false);
  // Organize / Scripts on a selection run server-side batches — one at a time.
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [groupByArtist, setGroupByArtist] = useState(false);
  const [gridSize, pickGridSize] = useGridSize();
  const [statsOpen, setStatsOpen] = useState(false);
  const [detailTrack, setDetailTrack] = useState<{ track: Track; albumPath: string } | null>(null);
  /** The album behind the open track dialog, when this payload has it: its
   *  `issues` map is where the full problem text lives (the track carries
   *  check codes only). */
  const detailAlbum = detailTrack
    ? (lib?.artists ?? []).flatMap((a) => a.albums).find((al) => al.path === detailTrack.albumPath)
    : undefined;
  const [bulkTagsOpen, setBulkTagsOpen] = useState(false);

  const [fullDates, setFullDates] = useLocalPref("full-dates", false);
  // User-added tag columns ride in the same visible/width prefs as the
  // built-ins: their Cols are appended to the view's defs (album rows read
  // the album's `meta` tags, track rows the files' own tags). The artist
  // table carries no tag record at all and the expanded album tracklist
  // shares its prefs with the album page (which has no tag-column code), so
  // both stay built-in-only.
  const [albumCustom, addAlbumCustomCol, removeAlbumCustomCol] = useCustomColumns("albums");
  const [trackCustom, addTrackCustomCol, removeTrackCustomCol] = useCustomColumns("tracks");
  const albumDefs: Col[] = [...ALBUM_COLS, ...customCols(albumCustom, "meta")];
  // The rating is the Library's own column and belongs beside the title (the
  // thing being rated), so it is spliced in there rather than appended: the
  // Columns menu and the row renderer both follow this order, and a column
  // that appears in a different place in each is a column that drifts.
  const trackDefs: Col[] = [
    ...TRACK_COLS.slice(0, 3),
    TRACK_RATING_COL,
    ...TRACK_COLS.slice(3),
    ...customCols(trackCustom, "tags"),
  ];
  const [albumCols, toggleAlbumCol] = useColumnPrefs("albums", albumDefs);
  const [artistCols, toggleArtistCol] = useColumnPrefs("artists", ARTIST_COLS);
  const [trackCols, toggleTrackCol] = useColumnPrefs("tracks", trackDefs);
  // A column the user just created should not start hidden.
  const addAlbumCustom = (tag: string, label?: string) => {
    const id = addAlbumCustomCol(tag, label);
    if (id) toggleAlbumCol(id);
  };
  const addTrackCustom = (tag: string, label?: string) => {
    const id = addTrackCustomCol(tag, label);
    if (id) toggleTrackCol(id);
  };
  // Album tracklists (the expanded album rows here share these prefs — and
  // their widths and tag columns — with the album page, since they are the
  // same table). New tag columns are added from the album page's own Columns
  // menu, where the tracklist is the primary table; they appear here too.
  const [alCustom, , removeAlCustomCol] = useCustomColumns("album-tracks");
  const alTrackDefs: Col[] = [...ALBUM_TRACK_COLS, ...customCols(alCustom, "tags")];
  const [alTrackCols, toggleAlTrackCol] = useColumnPrefs("album-tracks", alTrackDefs);
  const [alTrackW, setAlTrackW, resetAlTrackW] = useColumnWidths("album-tracks");
  // Drag-resized column widths, persisted per view ("Reset" in the Columns
  // menu — or double-click a handle — restores the fluid defaults).
  const [albumW, setAlbumW, resetAlbumW] = useColumnWidths("albums");
  const [artistW, setArtistW, resetArtistW] = useColumnWidths("artists");
  const [trackW, setTrackW, resetTrackW] = useColumnWidths("tracks");

  // One GET /api/ratings per scope for the whole page (react-query dedupes it
  // across every row, and the star controls share the cache). Declared HERE,
  // above the filter memo, because the rating facet filters on these maps: the
  // rows this page draws depend on them, so they are an input to the memo and
  // not a decoration applied afterwards. The album scope is what the album rows
  // draw — each album's OWN rating, a different fact from the ratings of the
  // tracks inside it.
  const { data: ratingsData } = useRatings();
  const { data: albumRatingsData } = useRatings("album");
  const ratings = ratingsData?.ratings;
  const albumRatings = albumRatingsData?.ratings;

  // Haystacks precomputed once per payload: the filter memo then only
  // does substring checks (no join/lowercase per keystroke).
  const flat = useMemo(() => {
    const albums: FlatAlbum[] = [];
    const tracks: FlatTrack[] = [];
    for (const a of lib?.artists ?? [])
      for (const al of a.albums) {
        // Prefer the tag-derived album artist (ALBUMARTIST/ARTIST); the
        // artist folder name is only a fallback, and the folder is named with
        // its MBID disambiguator ("Radiohead [a74b1b7f-…]") — so the folder's
        // own display name is what that fallback reads, never the raw basename.
        const artistName = al.album_artist || a.display_name || a.name;
        const trackHays: string[] = [];
        for (const t of al.tracks ?? []) {
          const hay = [artistName, al.meta?.ALBUM ?? "", t.file, ...Object.values(t.tags ?? {}).filter(Boolean).map(String)].join(" ").toLowerCase();
          trackHays.push(hay);
          tracks.push({ ...t, artist: artistName, album: al.meta?.ALBUM ?? al.path.split("/").pop() ?? "", albumCover: al.cover_file ?? null, albumPath: al.path, hay });
        }
        albums.push({
          ...al,
          artist: artistName,
          video_count: (al.tracks ?? []).filter((t) => t.is_video).length,
          inst_count: (al.tracks ?? []).filter((t) => t.tags.INSTRUMENTAL === "1").length,
          hay: [artistName, al.meta?.ALBUM, al.meta?.DATE, al.meta?.ARTIST, al.meta?.LABEL, al.meta?.CATALOGNUMBER, ...trackHays].join(" ").toLowerCase(),
        });
      }
    return { albums, tracks };
  }, [lib]);

  // Per-preset predicate, shared by the filter memo and the filter menu
  // counts (search text is applied separately from the preset).
  const trackPresetOK = (t: Track, preset: Preset) => {
    switch (preset) {
      case "all": return true;
      case "failing": return !t.grade_pass;
      case "cd": return (t.tags.MEDIA ?? "").toUpperCase().includes("CD");
      case "digital": return (t.tags.MEDIA ?? "").toUpperCase().includes("DIGITAL");
      case "instrumental": return t.tags.INSTRUMENTAL === "1";
      case "videos": return !!t.is_video;
      case "missingLyrics": return !t.lyrics_present;
    }
  };
  const albumPresetOK = (al: Album, preset: Preset) => {
    switch (preset) {
      case "all": return true;
      case "failing": return !al.pass;
      case "cd": return (al.media ?? "").toUpperCase().includes("CD");
      case "digital": return (al.media ?? "").toUpperCase().includes("DIGITAL");
      case "instrumental":
      case "videos":
      case "missingLyrics": return (al.tracks ?? []).some((t) => trackPresetOK(t, preset));
    }
  };

  // ---- the rating facet ("what have I not rated yet") ----
  //
  // A folder's stars and a file's stars are different facts (the store keeps
  // its own scope per path), so a row of either kind answers for what it IS:
  // a track is rated when its own file has stars, and an ALBUM only when the
  // user's verdict on the album is in (its folder rating) AND every track in
  // it carries one of its own — an album is finished, or it is not. One
  // starred track out of twelve used to mark the whole album done, which is
  // the reported "detecting … not just one": the facet's question is "what
  // have I not rated yet", and a half-rated album is exactly that. An artist
  // counts as rated when any of its albums does. That is the sentence
  // `RATED_NOTE` prints in the menu, and it is defined once here so the three
  // tables cannot each mean something else by the same word.
  const ratedTrack = (t: { path: string }) => ratingOf(ratings, t.path) > 0;
  const ratedAlbum = (al: Album) => {
    const tracks = al.tracks ?? [];
    return ratingOf(albumRatings, al.path) > 0 && tracks.every((t) => ratedTrack(t));
  };
  const ratingOK = (rated: boolean) =>
    ratingFilter === "any" ? true : ratingFilter === "rated" ? rated : !rated;

  // ---- the advisory facet ----
  //
  // The ladder is three-state (0 not explicit / 1 explicit / 2 clean edition —
  // see `AdvisoryBadge`), read here as the two questions a listener asks of it.
  // A track answers for itself; an album is explicit when ANY of its tracks is,
  // and clean only when NONE is — an album with one explicit track is an
  // explicit album, which is the direction that matters when the filter is
  // being used to keep that material away.
  const explicitTrack = (t: Track) => t.tags.ITUNESADVISORY === "1";
  const trackAdvisoryOK = (t: Track) =>
    advisoryFilter === "any" ? true
      : advisoryFilter === "explicit" ? explicitTrack(t)
        : !explicitTrack(t);
  const albumAdvisoryOK = (al: Album) => {
    if (advisoryFilter === "any") return true;
    const tracks = al.tracks ?? [];
    if (advisoryFilter === "explicit") return tracks.some(explicitTrack);
    return tracks.length > 0 && tracks.every((t) => !explicitTrack(t));
  };
  const presetCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const { id } of PRESETS) out[id] = flat.albums.filter((al) => albumPresetOK(al, id)).length;
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flat]);

  /** What each facet option would SHOW, counted over the view's own entity
   *  with the search and the presets applied but the facet itself open — a
   *  number that already had its own filter applied could never be anything
   *  but the current selection's size, and one that ignored the presets would
   *  promise rows the table is not going to draw.
   *
   *  The three tables count their own rows: an artist counts as rated /
   *  explicit when any of its albums is, the same cascade the rows themselves
   *  filter by (see `ratingOK` / `albumAdvisoryOK`). */
  const facetCounts = useMemo(() => {
    const words = parseQueryTerms(debouncedQuery).words;
    const hayOK = (hay: string) => words.every((w) => hay.includes(w));
    const albumFacets = (al: Album) => ({
      rated: ratedAlbum(al),
      explicit: (al.tracks ?? []).some(explicitTrack),
    });
    const rows: { rated: boolean; explicit: boolean }[] =
      view === "tracks"
        ? flat.tracks.filter((t) => hayOK(t.hay) && trackPresetOK(t, preset))
          .map((t) => ({ rated: ratedTrack(t), explicit: explicitTrack(t) }))
        : view === "artists"
          ? (lib?.artists ?? []).map((a) => {
            const mine = flat.albums.filter(
              (al) => a.albums.some((x) => x.path === al.path) && hayOK(al.hay) && albumPresetOK(al, preset));
            return { rated: mine.some(ratedAlbum), explicit: mine.some((al) => (al.tracks ?? []).some(explicitTrack)) };
          })
          : flat.albums.filter((al) => hayOK(al.hay) && albumPresetOK(al, preset)).map(albumFacets);
    const rated = rows.filter((r) => r.rated).length;
    const explicit = rows.filter((r) => r.explicit).length;
    return { rated, unrated: rows.length - rated, explicit, clean: rows.length - explicit };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flat, lib, view, debouncedQuery, preset, ratings, albumRatings]);

  // Album haystacks keyed by path (flat.albums covers every album in lib).
  const albumHay = useMemo(() => new Map(flat.albums.map((al) => [al.path, al.hay])), [flat]);

  const filtered = useMemo(() => {
    if (!lib) return { artists: [] as Artist[], albums: [] as FlatAlbum[], tracks: [] as FlatTrack[] };
    const terms = parseQueryTerms(debouncedQuery);
    const words = terms.words;
    const wordsMatch = (hay: string) => words.every((w) => hay.includes(w)); // hay already lowercase

    // tag-scoped term against any tags record ("#person" = any credited
    // person, "#any" = every tag, otherwise the exact canonical key)
    const tagTermOK = (rec: Record<string, unknown> | null | undefined, key: string, value: string) => {
      if (!rec) return false;
      if (key === "#any") return Object.values(rec).some((v) => v != null && String(v).toLowerCase().includes(value));
      if (key === "#person") return PERSON_TAG_KEYS.some((k) => String(rec[k] ?? "").toLowerCase().includes(value));
      return String(rec[key] ?? "").toLowerCase().includes(value);
    };
    const trackTagOK = (t: Track) =>
      terms.tags.every(({ key, value }) => tagTermOK(t.tags as Record<string, unknown>, key, value));

    const trOK = (t: FlatTrack) =>
      trackPresetOK(t, preset) && trackTagOK(t) && wordsMatch(t.hay)
      && ratingOK(ratedTrack(t)) && trackAdvisoryOK(t);

    const alOK = (al: Album) =>
      albumPresetOK(al, preset) && ratingOK(ratedAlbum(al)) && albumAdvisoryOK(al);
    const alTagOK = (al: Album) =>
      terms.tags.every(({ key, value }) =>
        tagTermOK((al.meta ?? {}) as Record<string, unknown>, key, value) ||
        (al.tracks ?? []).some((t) => tagTermOK(t.tags as Record<string, unknown>, key, value)));

    const artists: Artist[] = lib.artists
      .map((a) => ({ ...a, albums: a.albums.filter((al) => alOK(al) && wordsMatch(albumHay.get(al.path) ?? "") && alTagOK(al)) }))
      .filter((a) => a.albums.length);

    const albums = flat.albums.filter((al) => alOK(al) && wordsMatch(al.hay) && alTagOK(al));
    const tracks = flat.tracks.filter(trOK);
    return { artists, albums, tracks };
  }, [lib, debouncedQuery, preset, ratingFilter, advisoryFilter, ratings, albumRatings, flat, albumHay]);

  // ---- selection helpers ----
  const selTracks = useMemo(() => {
    const s = new Set(selection.tracks);
    for (const p of selection.albums) {
      const al = flat.albums.find((a) => a.path === p);
      for (const t of al?.tracks ?? []) s.add(t.path);
    }
    for (const p of selection.artists) {
      const a = lib?.artists.find((x) => x.path === p);
      for (const al of a?.albums ?? []) for (const t of al.tracks) s.add(t.path);
    }
    return s;
  }, [selection, flat, lib]);

  const selectionAlbumDirs = useMemo(() => {
    const dirs = new Set<string>();
    for (const p of selection.albums) dirs.add(p);
    for (const p of selection.artists) {
      const a = lib?.artists.find((x) => x.path === p);
      for (const al of a?.albums ?? []) dirs.add(al.path);
    }
    for (const p of selection.tracks) dirs.add(p.split("/").slice(0, -1).join("/"));
    return [...dirs];
  }, [selection, lib]);

  const selectionCount = selection.tracks.length + selection.albums.length + selection.artists.length;

  const addToPlaylist = async (paths: string[]) => {
    if (!paths.length) return;
    const pls = await api.playlists();
    const manual = pls.find((p) => p.kind === "manual");
    if (!manual) {
      const created = await api.createPlaylist("Library selection", "manual");
      await api.playlistAdd(created.id, paths);
    } else {
      await api.playlistAdd(manual.id, paths);
    }
    toast(`Added ${paths.length} track(s) to playlist`);
  };

  const removeAlbums = async (paths: string[]) => {
    if (!paths.length) return;
    const names = paths.map((d) => d.split("/").pop()).join(", ");
    if (!window.confirm(`Remove ${paths.length} album(s) from the library?\n${names}\n\nThey move to .mlo/trash in your music folder (recoverable).`)) return;
    setRemoving("batch");
    try {
      for (const d of paths) await api.removeAlbum(d);
      toast(`Moved ${paths.length} album(s) to trash`);
      clearSelection();
      invalidateLibrary(qc);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setRemoving(null);
    }
  };

  /** Auto-import lyrics for the selection through the provider chain.
   *
   * Skips instrumentals, videos and tracks that already have lyrics; the
   * backend runs the configured synced chain (LRCLIB → NetEase → Kugou →
   * QQ Music → Kuwo → YouTube captions), writes per the global
   * lyrics_format and canonicalizes like
   * script 13. Batched (100 tracks per request) so a big selection does not
   * hold one API worker thread for minutes. */
  const downloadLyricsSelection = async () => {
    if (!selectionCount) {
      toast("Select albums, artists or tracks first");
      return;
    }
    setLyricsBusy(true);
    try {
      const albumSet = new Set(selection.albums);
      const artistSet = new Set(selection.artists);
      const trackSet = new Set(selection.tracks);
      const targets = [
        ...flat.albums
          .filter((al) => albumSet.has(al.path))
          .flatMap((al) => (al.tracks ?? []).map((t) => t as Track)),
        ...(lib?.artists ?? [])
          .filter((a) => artistSet.has(a.path))
          .flatMap((a) => a.albums.flatMap((al) => al.tracks)),
        ...flat.tracks.filter((t) => trackSet.has(t.path)).map((t) => t as Track),
      ].filter((t) => t.tags.INSTRUMENTAL !== "1" && !t.lyrics_present && !t.is_video);
      if (!targets.length) {
        toast("Nothing to fetch — the selection has lyrics already (or is instrumental)");
        return;
      }
      let fetched = 0;
      let skipped = 0;
      let failed = 0;
      const providers: Record<string, number> = {};
      const chunk = 100;
      for (let i = 0; i < targets.length; i += chunk) {
        const paths = targets.slice(i, i + chunk).map((t) => t.path);
        const res = await api.lyricsAuto(paths);
        fetched += res.ok;
        skipped += res.skipped;
        failed += res.failed;
        for (const r of res.results) {
          if (r.status === "ok" && r.provider_label) {
            providers[r.provider_label] = (providers[r.provider_label] ?? 0) + 1;
          }
        }
      }
      const source = Object.entries(providers)
        .sort((a, b) => b[1] - a[1])
        .map(([label, n]) => `${label} ${n}`)
        .join(" · ");
      toast(
        `Lyrics: ${fetched} imported · ${skipped} skipped · ${failed} failed` +
          (source ? ` — ${source}` : "")
      );
      if (fetched) {
        clearSelection();
        invalidateLibrary(qc);
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLyricsBusy(false);
    }
  };

  const organizeSelection = async () => {
    if (busy) {
      toast("Still organizing the previous selection");
      return;
    }
    if (!selectionAlbumDirs.length) {
      toast("Select albums or artists to organize");
      return;
    }
    if (!window.confirm(`Organize ${selectionAlbumDirs.length} album(s) with the naming script from Settings?\nFiles are MOVED into the scripted folder structure.`)) return;
    setBusy(true);
    try {
      const r = await api.organize(selectionAlbumDirs);
      const moved = r.results.reduce((n: number, x: { moved?: number }) => n + (x.moved ?? 0), 0);
      const errs = r.results.filter((x: { error?: string }) => x.error);
      if (errs.length) toast(`Organized ${moved} file(s) — ${errs.length} album(s) had errors`);
      else toast(`Organized ${moved} file(s)`);
      clearSelection();
      invalidateLibrary(qc);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  // One GET /api/ratings per scope for the whole page (react-query dedupes it
  // across every row) and the optimistic setters the star controls share.
  // The maps themselves are declared above the filter memo (the facet reads
  // them); only the setters live here, beside the actions that call them.
  const { setRating, pending } = useSetRating();
  const { setRating: setAlbumRating, pending: albumPending } = useSetRating("album");

  const playSelection = () => {
    const out: { path: string; file: string; albumPath: string; artist?: string; album?: string; title?: string; coverFile?: string | null; albumCover?: string | null; advisory?: string | null }[] = [];
    for (const al of sortedAlbums)
      if (selection.albums.includes(al.path))
        for (const t of al.tracks) out.push({ path: t.path, file: t.file, albumPath: al.path, artist: al.artist, album: al.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined, coverFile: t.cover_file ?? null, albumCover: al.cover_file ?? null, advisory: t.tags.ITUNESADVISORY ?? null });
    for (const a of sortedArtists)
      if (selection.artists.includes(a.path))
        for (const al of a.albums)
          for (const t of al.tracks) out.push({ path: t.path, file: t.file, albumPath: al.path, artist: al.album_artist || a.display_name || a.name, album: al.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined, coverFile: t.cover_file ?? null, albumCover: al.cover_file ?? null, advisory: t.tags.ITUNESADVISORY ?? null });
    for (const tr of sortedTracks)
      if (selection.tracks.includes(tr.path))
        out.push({ path: tr.path, file: tr.file, albumPath: tr.path.split("/").slice(0, -1).join("/"), artist: tr.artist, album: tr.album, title: tr.tags.TITLE || undefined, coverFile: tr.cover_file ?? null, albumCover: tr.albumCover ?? null, advisory: tr.tags.ITUNESADVISORY ?? null });
    if (out.length) playNow(out);
  };

  const runScriptsOnSelection = async (ids: number[], force = false) => {
    if (busy) {
      toast("Still working on the previous selection");
      return;
    }
    if (!selectionAlbumDirs.length) {
      toast("Select albums or artists to run scripts on");
      return;
    }
    setBusy(true);
    try {
      // Same selection the header Force menu configures (Settings → General
      // force toggles keep working independently as saved defaults).
      const forceOpts = force ? forceDict(loadForceSel()) : undefined;
      await api.run(ids, selectionAlbumDirs, forceOpts);
      toast(`Scripts run on ${selectionAlbumDirs.length} album(s)${force ? " (forced)" : ""}`);
      invalidateLibrary(qc);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleExpand = (path: string) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  // The album's own rating, in the row's own shape: the store keeps it (a
  // folder has no RATING tag to read), and the album table sorts on what its
  // columns show — `sortRows` resolves a dotted key against the row — so the
  // albums the tables render carry it, like `video_count` above. Shallow
  // copies: the track lists stay shared.
  const ratedAlbums = useMemo(
    () => filtered.albums.map((al) => ({ ...al, rating: ratingOf(albumRatings, al.path) })),
    [filtered.albums, albumRatings]
  );

  // The tracks table's own Rating column reads the row, exactly like the
  // albums one: `sortRows` resolves the dotted sort key against the row, so the
  // star the column DRAWS is the value a click on its header sorts by. Shallow
  // copies (the tags and tech records stay shared).
  const ratedTracks = useMemo(
    () => filtered.tracks.map((t) => ({ ...t, rating: ratingOf(ratings, t.path) })),
    [filtered.tracks, ratings]
  );

  // Sorting is memoized so typing in the search box / toggling selection
  // doesn't re-sort the whole library on every keystroke.
  const sortedAlbums = useMemo(() => sortRows(ratedAlbums, albumSort), [ratedAlbums, albumSort]);
  const sortedArtists = useMemo(() => sortRows(filtered.artists, artistSort), [filtered.artists, artistSort]);
  const sortedTracks = useMemo(() => sortRows(ratedTracks, trackSort), [ratedTracks, trackSort]);
  // Where everything still being acquired is, from the queue's own rows and
  // the pushed job frames (lib/acquisition) — the SAME cache entry and the
  // same frames the Soulseek page draws, so one album cannot read as two
  // different stages. The queue is asked only while this page has something
  // pending; a settled library asks it nothing.
  const pendingHere = useMemo(() => flat.albums.some((al) => al.pending), [flat]);
  const acquisition = useAcquisitions(pendingHere);

  // Compact rows re-render on every selection toggle and their tracklist is
  // built inside a .map (no hook allowed there) — order each album's tracks
  // once here instead of re-sorting them on every render.
  const tracksByAlbum = useMemo(() => {
    const out: Record<string, Track[]> = {};
    for (const al of sortedAlbums) out[al.path] = [...(al.tracks ?? [])].sort(byDiscThenTrack);
    return out;
  }, [sortedAlbums]);

  // Grid sections: one flat list, or artist-headed groups.
  const gridSections = useMemo(() => {
    if (!groupByArtist) return [{ artist: null as string | null, albums: sortedAlbums }];
    const out: { artist: string | null; albums: FlatAlbum[] }[] = [];
    let cur: string | null = null;
    for (const al of sortedAlbums) {
      if (al.artist !== cur) {
        cur = al.artist;
        out.push({ artist: cur, albums: [] });
      }
      out[out.length - 1].albums.push(al);
    }
    return out;
  }, [sortedAlbums, groupByArtist]);

  if (error) return <EmptyState title="Backend unreachable" hint={String(error)} />;
  if (isLoading || !lib) return <PageLoading label="Scanning library…" />;

  // What the last layout scan actually found. A report describing a DIFFERENT
  // music folder (`stale`) is not this library's state, and no report at all
  // means no scan has run — neither may produce a warning, and the warning
  // may never be the only place a scan is claimed to have happened.
  const layoutProblems = layout?.exists && !layout.stale ? layout.report?.total ?? 0 : 0;
  const layoutKinds = layout?.exists && !layout.stale ? Object.keys(layout.report?.counts ?? {}).length : 0;

  const albumRows: ({ kind: "header"; artist: string } | { kind: "album"; album: FlatAlbum })[] = [];
  if (groupByArtist) {
    let current = "";
    for (const al of sortedAlbums) {
      if (al.artist !== current) {
        current = al.artist;
        albumRows.push({ kind: "header", artist: current });
      }
      albumRows.push({ kind: "album", album: al });
    }
  }

  const albumColSpan = 3 + albumCols.length + (selectMode ? 1 : 0); // checkbox?, chevron+cover, cols, actions
  // The rows the albums table actually draws: artist-headed groups when the
  // toggle is on, a flat list otherwise. Named because the table both renders
  // them and has to know whether there are any.
  const albumTableRows: ({ kind: "header"; artist: string } | { kind: "album"; album: FlatAlbum })[] =
    groupByArtist ? albumRows : sortedAlbums.map((al) => ({ kind: "album" as const, album: al }));
  // The other two tables' widths, for the same reason the album one exists:
  // their "nothing matches" row has to span the table it sits in.
  const artistColSpan = 1 + artistCols.length + (selectMode ? 1 : 0);
  const trackColSpan =
    trackDefs.filter((c) => trackCols.includes(c.id)).length + (selectMode ? 1 : 0);

  const allAlbumsSelected = sortedAlbums.length > 0 && sortedAlbums.every((a) => selection.albums.includes(a.path));
  const allArtistsSelected = sortedArtists.length > 0 && sortedArtists.every((a) => selection.artists.includes(a.path));
  const allTracksSelected = sortedTracks.length > 0 && sortedTracks.every((t) => selection.tracks.includes(t.path));

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      {/* toolbar rides in the header: controls left, stats/select/counts right */}
      <PageHeader icon={Library} title="Library">
      {/* The layout finding is the one condition that is about the whole
          library rather than an album: it says part of the music folder is
          not a graded album at all, which no per-album badge can show. The
          count and the moment it was measured come from the stored report,
          and the click goes straight to the panel that can act on it. */}
      {layoutProblems > 0 && (
        <Link
          to="/optimize"
          className="text-xs text-amber-200 bg-amber-950/30 border border-amber-900/60 rounded-lg px-3 py-2 flex items-start gap-2 tap"
          title="Open the Optimization page's library-layout panel"
        >
          <FolderTree className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span className="min-w-0">
            The last layout scan found <span className="font-mono">{layoutProblems}</span> problem
            {layoutProblems === 1 ? "" : "s"} across <span className="font-mono">{layoutKinds}</span> categor
            {layoutKinds === 1 ? "y" : "ies"} in the music folder
            {layout?.scanned_at ? ` (${new Date(layout.scanned_at).toLocaleString()})` : ""}. This one is
            library-wide, not an album's tags: whatever sits outside{" "}
            <span className="font-mono">Artists/&lt;Artist&gt;/&lt;Album&gt;/</span> is not graded at all,
            so the library does not grade clean until it is dealt with.{" "}
            <span className="text-amber-300/90 underline underline-offset-2">Review in Optimization →</span>
          </span>
        </Link>
      )}
      {/* toolbar — every control on ONE line (wrapped as a unit when the
          window is narrow): view tabs, sort, grid size, group-by, columns,
          quick filter — then stats/select and the counts on the right. */}
      <div className="flex items-center gap-2 flex-wrap">
        {/* Five view tabs are wider than a phone: they wrap inside their own
            box. `max-w-full` resolves against the toolbar (a block-level flex
            container, so it does bound them); the tabs' own `.tap` carries the
            phone target height. */}
        <Segmented value={view} onChange={setView} options={VIEW_TABS}
          className="max-w-full flex-wrap" />

        {(view === "albums" || view === "compact" || view === "grid") && (
          <div className="relative">
            <button
              className={`btn-ghost !py-1.5 text-xs tap ${sortOpen ? "!text-white !bg-raise" : ""}`}
              onClick={() => setSortOpen(!sortOpen)}
              title="Sort albums"
            >
              <ArrowDownUp className="h-3.5 w-3.5" />
              {albumSort ? `${ALBUM_SORTS.find((s) => s.key === albumSort.key)?.label ?? "Sort"} ${albumSort.dir === 1 ? "↑" : "↓"}` : "Sort"}
            </button>
            {sortOpen && (
              <>
                <div className="fixed inset-0 z-30" onClick={() => setSortOpen(false)} />
                <div className="absolute left-0 top-full mt-1 z-40 w-44 rounded-lg border border-border bg-zinc-950 shadow-2xl p-1.5">
                  {ALBUM_SORTS.map((s) => (
                    <button
                      key={s.key}
                      onClick={() => {
                        setAlbumSort(s.key);
                        setSortOpen(false);
                      }}
                      className={`w-full text-left px-2.5 py-1.5 rounded-md text-xs flex items-center justify-between gap-3 ${
                        albumSort?.key === s.key ? "bg-raise text-white" : "text-zinc-400 hover:text-white hover:bg-raise"
                      }`}
                    >
                      <span>{s.label}</span>
                      {albumSort?.key === s.key && <span className="font-mono">{albumSort.dir === 1 ? "↑" : "↓"}</span>}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {view === "grid" && (
          <span title="Cover size">
            <Segmented value={gridSize} onChange={pickGridSize} options={GRID_SIZES} />
          </span>
        )}

        {(view === "albums" || view === "grid") && (
          <button
            className={`btn-ghost !py-1.5 text-xs tap ${groupByArtist ? "!text-accent !border-accent/50" : ""}`}
            onClick={() => setGroupByArtist(!groupByArtist)}
            title="Group albums under artist headers"
          >
            <Layers className="h-3.5 w-3.5" /> Group by artist
          </button>
        )}

        {view !== "compact" && view !== "grid" && (
          <ColumnsMenu
            cols={view === "albums" ? albumDefs : view === "artists" ? ARTIST_COLS : trackDefs}
            visible={view === "albums" ? albumCols : view === "artists" ? artistCols : trackCols}
            onToggle={view === "albums" ? toggleAlbumCol : view === "artists" ? toggleArtistCol : toggleTrackCol}
            onAddCustom={view === "artists" ? undefined : view === "albums" ? addAlbumCustom : addTrackCustom}
            onRemoveCustom={view === "albums" ? removeAlbumCustomCol : view === "artists" ? undefined : removeTrackCustomCol}
            fullDates={fullDates}
            onFullDates={setFullDates}
            onResetWidths={view === "albums" ? () => { resetAlbumW(); resetAlTrackW(); } : view === "artists" ? resetArtistW : resetTrackW}
            hasCustomWidths={
              Object.keys(view === "albums" ? albumW : view === "artists" ? artistW : trackW).length > 0 ||
              (view === "albums" && Object.keys(alTrackW).length > 0)
            }
            extraCols={view === "albums" ? alTrackDefs : undefined}
            extraVisible={view === "albums" ? alTrackCols : undefined}
            onExtraToggle={view === "albums" ? toggleAlTrackCol : undefined}
            extraOnRemoveCustom={view === "albums" ? removeAlCustomCol : undefined}
          />
        )}

        {/* Quick filters: the presets plus the two facets. One menu, because
            they narrow the same table — and the count beside each row is what
            keeps a quick filter honest (it says how many rows it would leave
            BEFORE the click). */}
        <div className="relative">
          <button
            className={`btn-ghost !py-1.5 text-xs tap ${filterOpen ? "!text-white !bg-raise" : ""}`}
            onClick={() => setFilterOpen(!filterOpen)}
            title="Filter the library — presets, your star ratings, and explicit/clean"
          >
            <ListFilter className="h-3.5 w-3.5" />
            {[
              PRESETS.find((p) => p.id === preset)?.label,
              ratingFilter !== "any" ? RATING_FILTERS.find((f) => f.id === ratingFilter)?.label : null,
              advisoryFilter !== "any" ? ADVISORY_FILTERS.find((f) => f.id === advisoryFilter)?.label : null,
            ].filter(Boolean).join(" · ")}
            {(ratingFilter !== "any" || advisoryFilter !== "any") && (
              <span className="h-1.5 w-1.5 rounded-full bg-accent inline-block" title="Filters are active" />
            )}
          </button>
          {filterOpen && (
            <>
              <div className="fixed inset-0 z-30" onClick={() => setFilterOpen(false)} />
              <div className="absolute left-0 top-full mt-1 z-40 w-64 rounded-lg border border-border bg-zinc-950 shadow-2xl p-1.5 max-h-[70vh] overflow-y-auto">
                <FilterGroup label="Show">
                  {PRESETS.map((p) => (
                    <FilterRow
                      key={p.id}
                      label={p.label}
                      count={presetCounts[p.id] ?? 0}
                      active={preset === p.id}
                      onClick={() => setPreset(p.id)}
                    />
                  ))}
                </FilterGroup>
                <FilterGroup label="Star rating" note={RATED_NOTE}>
                  {RATING_FILTERS.map((f) => (
                    <FilterRow
                      key={f.id}
                      label={f.label}
                      hint={f.hint}
                      count={f.id === "any" ? facetCounts.rated + facetCounts.unrated : facetCounts[f.id]}
                      active={ratingFilter === f.id}
                      onClick={() => setRatingFilter(f.id)}
                    />
                  ))}
                </FilterGroup>
                <FilterGroup label="Advisory">
                  {ADVISORY_FILTERS.map((f) => (
                    <FilterRow
                      key={f.id}
                      label={f.label}
                      hint={f.hint}
                      count={f.id === "any" ? facetCounts.explicit + facetCounts.clean : facetCounts[f.id]}
                      active={advisoryFilter === f.id}
                      onClick={() => setAdvisoryFilter(f.id)}
                    />
                  ))}
                </FilterGroup>
                {(preset !== "all" || ratingFilter !== "any" || advisoryFilter !== "any") && (
                  <button
                    className="w-full text-left px-2.5 py-1.5 rounded-md text-xs text-zinc-500 hover:text-white hover:bg-raise"
                    onClick={() => {
                      setPreset("all");
                      setRatingFilter("any");
                      setAdvisoryFilter("any");
                      setFilterOpen(false);
                    }}
                  >
                    Clear all filters
                  </button>
                )}
              </div>
            </>
          )}
        </div>

        {/* The page's own search box: it edits the store value the top bar's
            box already edits (`useStore`'s query), so the toolbar can narrow
            the view without the user reaching back up to the app bar — and
            the two boxes cannot fork into two filters, which is the whole
            reason the query lives in the store. `key:value` terms match tags
            (artist:, genre:, year:, composer:) exactly as they do there. */}
        <div className="search-field relative flex-1 min-w-[9rem] max-w-xs">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-500" />
          <input
            className="input !py-1.5 !pl-8 text-xs"
            placeholder="Search — words + tags (artist:, genre:)"
            aria-label="Search the library"
            title="Plain words match album, artist, track, file and tag text; key:value matches one tag (artist: name · genre: metal · year: 1998). The same query as the top bar's box."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              // The row is no form: Enter must not submit anything, and the
              // caret stays in the box so a filter is typed in one pass.
              if (e.key === "Enter") e.preventDefault();
              else if (e.key === "Escape" && query) setQuery("");
            }}
          />
          {query && (
            <button
              className="absolute right-1.5 top-1/2 -translate-y-1/2 p-1 rounded text-zinc-500 hover:text-white tap"
              onClick={() => setQuery("")}
              title="Clear the search"
              aria-label="Clear the search"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>

        {/* min-w-0 + wrap: the counts grow with the library, so on a phone this
            group takes its own line instead of pushing the row past the edge. */}
        <div className="ml-auto flex items-center gap-2 flex-wrap min-w-0">
          <button
            className="btn-ghost !py-1.5 text-xs tap"
            onClick={() => setStatsOpen(true)}
            title={selectionCount ? "Statistics for the current selection" : "Library-wide statistics"}
          >
            <BarChart3 className="h-3.5 w-3.5" /> Stats
          </button>
          <button
            className={`btn-ghost !py-1.5 text-xs tap ${selectMode ? "!text-accent !border-accent/50" : ""}`}
            onClick={toggleSelectMode}
            title="Select mode — show checkboxes for batch actions"
          >
            <ListChecks className="h-3.5 w-3.5" /> Select
          </button>

          {/* Refresh: re-walk the music folder. Home carries the same control
              in the same order (Stats, Select, Refresh), so the two pages that
              draw the library cannot offer different ways to update it. */}
          <button
            className="btn-ghost !py-1.5 text-xs tap"
            onClick={refresh}
            disabled={isFetching}
            title="Look at the music folder again — drops the server's cached tree (and the tag reads under it) and re-walks it, then repaints this page"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> Refresh
          </button>

          <span className="text-xs text-zinc-500 whitespace-nowrap">
            {sortedAlbums.length} albums · {sortedTracks.length} tracks
          </span>
        </div>
      </div>
      </PageHeader>

      {/* selection toolbar */}
      {selectionCount > 0 && (
        <div className="flex items-center gap-2 bg-accent/15 border border-accent/40 rounded-lg px-3 py-2 flex-wrap">
          <span className="text-xs font-medium text-accent-soft">
            {selection.albums.length} album{selection.albums.length === 1 ? "" : "s"} · {selection.artists.length} artist{selection.artists.length === 1 ? "" : "s"} · {selection.tracks.length} track{selection.tracks.length === 1 ? "" : "s"} · {selTracks.size} total tracks
          </span>
          <div className="ml-auto flex gap-1.5 flex-wrap">
            <button className="btn-primary !py-1 text-xs tap" onClick={playSelection}>
              <Play className="h-3.5 w-3.5" /> Play
            </button>
            <button className="btn-ghost !py-1 text-xs tap" onClick={() => addToPlaylist([...selTracks])}>
              <ListPlus className="h-3.5 w-3.5" /> Playlist
            </button>
            {(selection.albums.length > 0 || selection.artists.length > 0) && (
              <button
                className="btn-danger !py-1 text-xs tap"
                onClick={() => removeAlbums(selectionAlbumDirs)}
                disabled={busy || removing === "batch" || !selectionAlbumDirs.length}
                title="Move selected albums to trash"
              >
                <Trash2 className="h-3.5 w-3.5" /> Remove
              </button>
            )}
            <ScriptsDropdown onRun={runScriptsOnSelection} runAllIds={runAllIds} />
            <button
              className="btn-ghost !py-1 text-xs tap"
              onClick={downloadLyricsSelection}
              disabled={lyricsBusy}
              title="Auto-import missing lyrics for the selection through the provider chain (skips instrumentals)"
            >
              <CloudDownload className="h-3.5 w-3.5" /> {lyricsBusy ? "Fetching…" : "Lyrics"}
            </button>
            <button
              className="btn-ghost !py-1 text-xs tap"
              onClick={() => setBulkTagsOpen(true)}
              disabled={!selTracks.size}
              title="Bulk remove or set tags on the selected tracks"
            >
              <Tag className="h-3.5 w-3.5" /> Tags
            </button>
            <button
              className="btn-ghost !py-1 text-xs tap"
              onClick={organizeSelection}
              disabled={busy || removing === "batch" || !selectionAlbumDirs.length}
              title="Apply the naming script from Settings"
            >
              <FolderSync className="h-3.5 w-3.5" /> {busy ? "Organizing…" : "Organize"}
            </button>
            <button className="btn-ghost !py-1 text-xs tap" onClick={clearSelection}>
              Clear
            </button>
          </div>
        </div>
      )}

      {sortedAlbums.length === 0 && (
        <EmptyState title="Nothing matches" hint="Set the music folder in Settings, import an album, or clear the search/filters." />
      )}

      {statsOpen && (
        <StatsPanel
          title={
            selectionCount
              ? `${selTracks.size} selected track${selTracks.size === 1 ? "" : "s"}`
              : "whole library"
          }
          albums={selectionCount ? flat.albums.filter((a) => selectionAlbumDirs.includes(a.path)) : flat.albums}
          tracks={selectionCount ? flat.tracks.filter((t) => selTracks.has(t.path)) : flat.tracks}
          /* The library's layout condition, only for the whole-library
             readout: a selection's grade says nothing about the music folder
             it was picked from, and the report is always about all of it. */
          layout={!selectionCount && layout?.exists && !layout.stale && layout.report
            ? { total: layout.report.total, counts: layout.report.counts, scanned_at: layout.scanned_at }
            : undefined}
          onClose={() => setStatsOpen(false)}
        />
      )}

      {bulkTagsOpen && (
        <BulkTagsDialog
          paths={[...selTracks]}
          onClose={() => {
            setBulkTagsOpen(false);
            invalidateLibrary(qc);
          }}
        />
      )}

      {detailTrack && (
        <TrackDetails
          track={detailTrack.track}
          albumPath={detailTrack.albumPath}
          onClose={() => setDetailTrack(null)}
          /* Same wiring as the album page: the full problem text lives on the
             album, the track carries only the check codes. Empty when this
             payload has no album for the track (the modal keeps the codes). */
          messages={Object.entries(detailAlbum?.issues ?? {})
            .filter(([, files]) => files.includes(detailTrack.track.file))
            .map(([text]) => text)}
        />
      )}

      {/* ---------------- Grid browse view (Apple Music style, default) ---------------- */}
      {view === "grid" && (
        <div>
          <div
            className="grid gap-x-4 gap-y-5 stagger"
            style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize]}px, 1fr))` }}
          >
            {gridSections.map((sec) => (
              <Fragment key={sec.artist ?? "all"}>
                {sec.artist !== null && (
                  <div className="col-span-full mt-3 first:mt-0">
                    <div className="text-sm font-bold tracking-wide text-zinc-300">{sec.artist}</div>
                    <div className="h-px bg-border mt-1" />
                  </div>
                )}
                {sec.albums.map((al) => {
                  const sel = selection.albums.includes(al.path);
                  return (
                    <AlbumCard
                      key={al.path}
                      al={al}
                      selectable={selectMode}
                      selected={sel}
                      onSelect={toggleAlbum}
                      extraMeta={al.pending ? <AcquisitionChip acq={acquisition(al.path, al.wish_id)} /> : null}
                    />
                  );
                })}
              </Fragment>
            ))}
          </div>
          <div className="h-2" />
        </div>
      )}

      {/* ---------------- Compact status view ---------------- */}
      {view === "compact" && (
        <div className="space-y-1">
          <div className="flex gap-4 flex-wrap text-[10px] text-zinc-600 items-center pb-1">
            <span className="inline-flex items-center gap-1.5"><span className="h-1.5 w-1.5 rounded-full bg-emerald-600/60 inline-block" /> PASS — graded clean, audit OK</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-1.5 w-1.5 rounded-full bg-red-500/70 inline-block" /> FAIL — grading / audit problems (hover a row for details)</span>
          </div>
          {sortedAlbums.map((al) => {
            const st = statusFor(!!al.pass, al.audit_summary);
            const sel = selection.albums.includes(al.path);
            const isExp = expanded.has(al.path);
            const tracks = tracksByAlbum[al.path] ?? [];
            return (
              <div key={al.path}>
                <div
                  className={`group flex items-center gap-2.5 rounded-md px-2 py-1.5 cursor-pointer transition-colors ${st.tint} ${sel ? "bg-accent/10" : "hover:bg-white/[0.06]"}`}
                  onClick={() => (selectMode ? toggleAlbum(al.path) : toggleExpand(al.path))}
                >
                  <div className={`w-1 self-stretch rounded-sm ${st.edge} shrink-0`} title={st.label} />
                  {selectMode && (
                    <div className="shrink-0" onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" checked={sel} onChange={() => toggleAlbum(al.path)} />
                    </div>
                  )}
                  <Link
                    to={albumRef(al)}
                    onClick={(e) => e.stopPropagation()}
                    className="shrink-0"
                    title="Open album page"
                  >
                    <CoverImg
                      albumPath={al.path}
                      coverFile={al.cover_file}
                      wrapperClass="h-9 w-9 rounded bg-raise overflow-hidden shrink-0"
                    />
                  </Link>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2 min-w-0">
                      <Link
                        to={albumRef(al)}
                        onClick={(e) => e.stopPropagation()}
                        className="text-sm font-medium truncate hover:text-accent-soft"
                        title={al.meta?.ALBUM ?? al.path}
                      >
                        {al.meta?.ALBUM ?? al.path.split("/").pop()}
                      </Link>
                      <AdvisoryMark value={al.meta?.ITUNESADVISORY ?? al.meta?.ALBUMITUNESADVISORY} />
                      {/* The folder itself is held (a run, an import, an
                          organize): its files are not playable right now. */}
                      <LockedChip path={al.path} />
                      {/* Added, not downloaded yet: the row says which state
                          the album is in and why, beside its (empty) track
                          count. `label` — the compact list has room to say
                          it outright rather than only on hover. */}
                      <PendingMark album={al} label />
                      <AcquisitionChip acq={al.pending ? acquisition(al.path, al.wish_id) : null} />
                      <span className="text-[11px] text-zinc-500 truncate">
                        {al.artist}
                        {al.meta?.ORIGINALDATE || al.meta?.DATE ? ` · ${originalYear(al.meta)}` : ""}
                        {al.meta?.DATE && al.meta?.ORIGINALDATE && String(al.meta.ORIGINALDATE).slice(0, 4) !== String(al.meta?.DATE ?? "").slice(0, 4)
                          ? ` (rel. ${String(al.meta.DATE).slice(0, 4)})` : ""}
                      </span>
                    </div>
                  </div>
                  <span className={`text-[9px] font-mono shrink-0 ${st.text}`} title={st.label}>
                    {st.key === "fail" ? gradeSliver(!!al.pass, al.audit_summary) : ""}
                  </span>
                  {/* the album's OWN rating, like the star row a track holds one
                      level down — a verdict on the album, not the average of its
                      tracks (and never a tag: a folder has none) */}
                  <StarRating
                    size="sm"
                    label="Album rating"
                    hint={`Your rating for the album. ${FOLDER_RATING_NOTE}`}
                    value={al.rating}
                    onChange={(v) => setAlbumRating(al.path, v)}
                    pending={albumPending(al.path)}
                  />
                  <span className="text-[10px] text-zinc-600 shrink-0 w-8 text-right">{al.track_count}t</span>
                  <div className="opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 flex gap-1 shrink-0 transition-opacity" onClick={(e) => e.stopPropagation()}>
                    <button
                      className="btn-ghost !px-1.5 !py-0.5 tap"
                      title={isExp ? "Collapse" : "Show tracks"}
                      onClick={() => toggleExpand(al.path)}
                    >
                      {isExp ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                </div>
                {isExp && (
                  <div className="ml-8 border-l border-border pl-3 py-1 space-y-0.5">
                    {tracks.map((t) => {
                      const ts = statusFor(!!t.grade_pass, t.audit);
                      const tSel = selection.tracks.includes(t.path);
                      return (
                        <div
                          key={t.path}
                          className={`group flex items-center gap-2 text-xs py-0.5 rounded cursor-pointer ${tSel ? "bg-accent/10" : "hover:bg-white/[0.06]"}`}
                          onClick={selectMode ? () => toggleTrack(t.path) : () =>
                            playNow(
                              tracks.map((x) => ({ path: x.path, file: x.file, albumPath: al.path, artist: al.artist, album: al.meta?.ALBUM ?? undefined, title: x.tags.TITLE || undefined, coverFile: x.cover_file ?? null, albumCover: al.cover_file ?? null, advisory: x.tags.ITUNESADVISORY ?? null })),
                              tracks.findIndex((x) => x.path === t.path)
                            )
                          }
                          title={selectMode ? "Click to select" : "Click to play"}
                        >
                          {selectMode && (
                            <div className="shrink-0">
                              <input type="checkbox" checked={tSel} onChange={() => toggleTrack(t.path)} />
                            </div>
                          )}
                          <span className={`h-3 w-1 rounded-sm ${ts.edge} shrink-0`} title={ts.label} />
                          <span className="w-10 text-right text-zinc-600 font-mono shrink-0 cell-nowrap">{t.tracknumber ?? t.tags.TRACKNUMBER ?? "—"}</span>
                          <TrackCover
                            albumPath={al.path}
                            trackCover={t.cover_file}
                            albumCover={al.cover_file}
                            wrapperClass="h-8 w-8 rounded bg-raise overflow-hidden shrink-0"
                          />
                          <TrackTitleCell
                            className="flex-1"
                            trailing={
                              <>
                                <span className="shrink-0"><FavHeart kind="track" id={t.path} mbid={t.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" revealOnHover /></span>
                                <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                                  <TrackActionsMenu path={t.path} releaseMbid={t.tags.MUSICBRAINZ_ALBUMID} />
                                </span>
                                <StarRating size="sm" value={ratingOf(ratings, t.path)} onChange={(v) => setRating(t.path, v)} pending={pending(t.path)} />
                                <span className="text-[10px] text-zinc-600 font-mono w-10 text-right shrink-0 cell-nowrap">{fmtDuration(t.tech.length)}</span>
                              </>
                            }
                          >
                            <Link to={trackRef(t)} className="break-words hover:text-accent-soft min-w-0"
                              title="Click to play · Ctrl-click to open track page"
                              onClick={(e) => entityLinkClick(e, () => navigate(trackRef(t)))}
                            >
                              {t.tags.TITLE ?? t.file}
                            </Link>
                            <AdvisoryMark value={t.tags.ITUNESADVISORY} />
                            <LockedChip path={t.path} />
                            {!!t.issues?.length && (
                              <button
                                className="text-[9px] text-red-400/70 shrink-0 hover:text-red-300"
                                title={t.issues.join("\n")}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setDetailTrack({ track: t, albumPath: al.path });
                                }}
                              >
                                {t.issues.length}✗
                              </button>
                            )}
                            <GradeBadge pass={!!t.grade_pass && !auditFails(t.audit)} size="sm" />
                            <CachedMark path={t.path} />
                            {t.tags.INSTRUMENTAL === "1" && (
                              <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[9px] shrink-0">INST</span>
                            )}
                          </TrackTitleCell>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* ---------------- Albums table ---------------- */}
      {view === "albums" && (
        <div>
          <div className="overflow-x-auto">
            <table className={`${TABLE_FIT} text-sm`}>
              <thead className="border-b border-border">
                <tr>
                  {selectMode && (
                    <th className="th w-8">
                      <input type="checkbox" className="" checked={allAlbumsSelected}
                        onChange={() => setSelection({ albums: allAlbumsSelected ? [] : sortedAlbums.map((a) => a.path) })} />
                    </th>
                  )}
                  {/* Both leading cells head a control rather than a column of
                      values (the expand chevron, the cover); the Tracks view
                      names its cover cell the same way, so the header row is
                      not two blank announcements to a screen reader. */}
                  <th className="th w-10"><span className="sr-only">Expand</span></th>
                  <th className="th w-16"><span className="sr-only">Cover</span></th>
                  {albumDefs.filter((c) => albumCols.includes(c.id)).map((c) => (
                    <SortHeader key={c.id} label={c.label} sort={albumSort} sortKey={c.sortKey} onSort={setAlbumSort}
                      className={`relative ${ALBUM_COL_W[c.id] ?? (c.tag ? TAG_COL_W : "")}${phoneHide(ALBUM_PHONE_CLS, c.id)}`}
                      style={albumW[c.id] ? { width: albumW[c.id] } : undefined} >
                      <ColumnResizer width={albumW[c.id]} onDrag={(w) => setAlbumW(c.id, w)} onReset={() => resetAlbumW()} />
                    </SortHeader>
                  ))}
                  <th className="th w-24 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="stagger">
                {albumTableRows.length === 0 && (
                  /* A filtered-to-nothing table used to render as a header row
                     over blank space, which reads as a broken view rather than
                     as an answer — this is the answer, and it says where the
                     filters are. */
                  <tr>
                    <td colSpan={albumColSpan} className="td text-zinc-500">
                      No albums match these filters — clear them in the Filter menu.
                    </td>
                  </tr>
                )}
                {albumTableRows.map((row) =>
                  row.kind === "header" ? (
                    <tr key={`h-${row.artist}`} className="bg-panel/70">
                      <td colSpan={albumColSpan} className="px-3 py-1.5 text-xs font-bold tracking-wide text-zinc-300">
                        {row.artist}
                      </td>
                    </tr>
                  ) : (
                    <AlbumRowGroup
                      key={row.album.path}
                      album={row.album}
                      acq={row.album.pending ? acquisition(row.album.path, row.album.wish_id) : null}
                      expanded={expanded.has(row.album.path)}
                      onToggle={() => toggleExpand(row.album.path)}
                      visibleCols={albumCols}
                      selected={selection.albums.includes(row.album.path)}
                      onToggleSel={() => toggleAlbum(row.album.path)}
                      selTracks={selTracks}
                      onToggleTrack={toggleTrack}
                      removing={removing !== null}
                      onRemove={() => removeAlbums([row.album.path])}
                      onPlaylist={() => addToPlaylist(row.album.tracks.map((t) => t.path))}
                      onTrackDetails={(t) => setDetailTrack({ track: t, albumPath: row.album.path })}
                      colSpan={albumColSpan}
                      fullDates={fullDates}
                      selectMode={selectMode}
                      trackCols={alTrackCols}
                      trackCustom={alCustom}
                      tagCols={albumCustom}
                      trackWidths={alTrackW}
                      onTrackWidth={(id, w) => setAlTrackW(id, w)}
                      onResetTrackWidths={resetAlTrackW}
                    />
                  )
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ---------------- Artists table ---------------- */}
      {view === "artists" && (
        <div>
          <div className="overflow-x-auto">
            <table className={`${TABLE_FIT} text-sm`}>
              <thead className="border-b border-border">
                <tr>
                  {selectMode && (
                    <th className="th w-8">
                      <input type="checkbox" className="" checked={allArtistsSelected}
                        onChange={() => setSelection({ artists: allArtistsSelected ? [] : sortedArtists.map((a) => a.path) })} />
                    </th>
                  )}
                  {/* The row's own name gets the floor the album table gives its
                      album column — the counter columns beside it fold on a phone,
                      this one never does, so its floor only applies from `md`. */}
                  <th className="th md:w-[220px]">Artist</th>
                  {ARTIST_COLS.filter((c) => artistCols.includes(c.id)).map((c) => (
                    <SortHeader key={c.id} label={c.label} sort={artistSort} sortKey={c.sortKey} onSort={setArtistSort}
                      className={`relative ${ARTIST_COL_W[c.id] ?? TAG_COL_W}${phoneHide(ARTIST_PHONE_CLS, c.id)}`}
                      style={artistW[c.id] ? { width: artistW[c.id] } : undefined}>
                      <ColumnResizer width={artistW[c.id]} onDrag={(w) => setArtistW(c.id, w)} onReset={() => resetArtistW()} />
                    </SortHeader>
                  ))}
                </tr>
              </thead>
              <tbody className="stagger">
                {sortedArtists.length === 0 && (
                  <tr>
                    <td colSpan={artistColSpan} className="td text-zinc-500">
                      No artists match these filters — clear them in the Filter menu.
                    </td>
                  </tr>
                )}
                {sortedArtists.map((a) => {
                  const sel = selection.artists.includes(a.path);
                  return (
                    <tr
                      key={a.path}
                      className={`table-row group ${sel ? "bg-accent/15" : ""}`}
                      title={selectMode ? "Click to select" : "Open the artist page"}
                      /* `.table-row` promises a click — pointer cursor, hover
                         wash — and the row now answers it: it opens the
                         artist, or ticks the row while select mode is on,
                         the same deal the album and track rows make. */
                      onClick={selectMode ? () => toggleArtist(a.path) : () => navigate(artistRef(a))}
                    >
                      {selectMode && (
                        <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                          <input type="checkbox" className="" checked={sel} onChange={() => toggleArtist(a.path)} />
                        </td>
                      )}
                      <td className="td">
                        {/* the row click already opens the artist, so the link
                            must not push the same route a second time. The
                            NAME is the folder's display name: the folder is
                            named "Radiohead [a74b1b7f-…]", and the raw basename
                            was what this table used to print. */}
                        <Link to={artistRef(a)} onClick={(e) => e.stopPropagation()} className="font-medium hover:text-accent-soft">
                          {a.display_name || a.name}
                        </Link>
                      </td>
                      {artistCols.includes("albums") && (
                        <td className={`td text-zinc-500${phoneHide(ARTIST_PHONE_CLS, "albums")}`}>{a.aggregate.album_count}</td>
                      )}
                      {artistCols.includes("tracks") && (
                        <td className={`td text-zinc-500${phoneHide(ARTIST_PHONE_CLS, "tracks")}`}>{a.aggregate.track_count}</td>
                      )}
                      {artistCols.includes("checks") && (
                        <td className={`td text-zinc-500${phoneHide(ARTIST_PHONE_CLS, "checks")}`}>{a.aggregate.pass_count}/{a.aggregate.total_checks}</td>
                      )}
                      {artistCols.includes("grade") && (
                        /* `aggregate.pass` (failed checks == 0), not a rounded
                           `grade_pct >= 100`: rounding could draw a green dot
                           over an artist whose album failed a check. */
                        <td className="td"><GradeBadge pass={!!a.aggregate.pass && !auditFails(a.aggregate.audit_summary)} score={a.aggregate.grade_pct} /></td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ---------------- Tracks table ---------------- */}
      {view === "tracks" && (
        <div>
          <div className="overflow-x-auto">
            <table className={`${TABLE_FIT} text-sm`}>
              <thead className="border-b border-border">
                <tr>
                  {selectMode && (
                    <th className="th w-8">
                      <input type="checkbox" className="" checked={allTracksSelected}
                        onChange={() => setSelection({ tracks: allTracksSelected ? [] : sortedTracks.map((t) => t.path) })} />
                    </th>
                  )}
                  {trackDefs.filter((c) => trackCols.includes(c.id)).map((c) => (
                    c.id === "cover" ? (
                      <th key={c.id} className={`th relative ${TRACK_COL_W[c.id] ?? ""}`} title="Cover art">
                        <span className="sr-only">Cover</span>
                      </th>
                    ) : (
                    <SortHeader key={c.id} label={c.label} sort={trackSort} sortKey={c.sortKey} onSort={setTrackSort}
                      className={`relative ${TRACK_COL_W[c.id] ?? (c.tag ? TAG_COL_W : "")}${phoneHide(TRACK_PHONE_CLS, c.id)}`}
                      style={trackW[c.id] ? { width: trackW[c.id] } : undefined}>
                      <ColumnResizer width={trackW[c.id]} onDrag={(w) => setTrackW(c.id, w)} onReset={() => resetTrackW()} />
                    </SortHeader>
                    )
                  ))}
                </tr>
              </thead>
              <tbody className="stagger">
                {sortedTracks.length === 0 && (
                  <tr>
                    <td colSpan={trackColSpan} className="td text-zinc-500">
                      No tracks match these filters — clear them in the Filter menu.
                    </td>
                  </tr>
                )}
                {sortedTracks.map((tr) => {
                  const sel = selection.tracks.includes(tr.path);
                  return (
                    <tr
                      key={tr.path}
                      className={`table-row group cursor-pointer ${sel ? "bg-accent/15" : ""}`}
                      title={selectMode ? "Click to select" : "Click to play"}
                      onClick={selectMode ? () => toggleTrack(tr.path) : () =>
                        playNow(
                          sortedTracks.map((t) => ({ path: t.path, file: t.file, albumPath: t.path.split("/").slice(0, -1).join("/"), artist: t.artist, album: t.album, title: t.tags.TITLE || undefined, coverFile: t.cover_file ?? null, albumCover: t.albumCover ?? null, advisory: t.tags.ITUNESADVISORY ?? null })),
                          sortedTracks.findIndex((t) => t.path === tr.path)
                        )
                      }
                    >
                      {selectMode && (
                        <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                          <input type="checkbox" className="" checked={sel} onChange={() => toggleTrack(tr.path)} />
                        </td>
                      )}
                      {trackCols.includes("num") && <td className={`td cell-nowrap text-zinc-600${phoneHide(TRACK_PHONE_CLS, "num")}`}>{tr.tracknumber ?? tr.tags.TRACKNUMBER ?? "—"}</td>}
                      {trackCols.includes("cover") && (
                        <td className="td cell-cover pr-0">
                          <TrackCover
                            albumPath={tr.albumPath ?? tr.path.split("/").slice(0, -1).join("/")}
                            trackCover={tr.cover_file}
                            albumCover={tr.albumCover}
                            wrapperClass="h-9 w-9 rounded bg-raise overflow-hidden shrink-0"
                          />
                        </td>
                      )}
                      {trackCols.includes("title") && (
                        <td className="td">
                          <TrackTitleCell
                            trailing={
                              <>
                                <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
                                  <FavHeart kind="track" id={tr.path} mbid={tr.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" revealOnHover />
                                </span>
                                <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                                  <TrackActionsMenu path={tr.path} releaseMbid={tr.tags.MUSICBRAINZ_ALBUMID} />
                                </span>
                                <button
                                  className="text-zinc-500 hover:text-accent-soft shrink-0"
                                  title="Grading & audit details"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setDetailTrack({ track: tr, albumPath: tr.path.split("/").slice(0, -1).join("/") });
                                  }}
                                >
                                  <InfoIcon className="h-3.5 w-3.5" />
                                </button>
                              </>
                            }
                          >
                            <Link
                              to={trackRef(tr)}
                              className="hover:text-accent-soft break-words min-w-0"
                              title="Click to play · Ctrl-click to open track page"
                              onClick={(e) => entityLinkClick(e, () => navigate(trackRef(tr)))}
                            >
                              {tr.tags.TITLE ?? tr.file}
                            </Link>
                            <AdvisoryMark value={tr.tags.ITUNESADVISORY} />
                            <LockedChip path={tr.path} />
                            {!!tr.issues?.length && (
                              <button
                                className="text-[9px] text-red-400/70 shrink-0 hover:text-red-300"
                                title={`${tr.issues.join("\n")}\nClick for details`}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setDetailTrack({ track: tr, albumPath: tr.path.split("/").slice(0, -1).join("/") });
                                }}
                              >
                                {tr.issues.length}✗
                              </button>
                            )}
                            <GradeBadge pass={!!tr.grade_pass && !auditFails(tr.audit)} size="sm" />
                            <CachedMark path={tr.path} />
                            {tr.is_video && <span title="Music video" className="shrink-0 inline-flex"><FileVideo className="h-3.5 w-3.5 text-zinc-500" /></span>}
                            {tr.tags.INSTRUMENTAL === "1" && (
                              <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[10px] shrink-0">INST</span>
                            )}
                          </TrackTitleCell>
                        </td>
                      )}
                      {/* The rating has its own column here, and that is the
                          fix for the collapse this view shipped: the stars used
                          to ride in the title cell's trailing slot, which made
                          one narrow fixed-layout column hold the name AND the
                          marks AND the stars — 220 px against ~200 px of
                          controls, so the title lost and rendered one character
                          per line. A column is also what a reader wants: a
                          straight vertical scan of the ratings (the album
                          table has drawn it this way all along). */}
                      {trackCols.includes("rating") && (
                        <td
                          className={`td${phoneHide(TRACK_PHONE_CLS, "rating")}`}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <StarRating
                            size="sm"
                            value={ratingOf(ratings, tr.path)}
                            onChange={(v) => setRating(tr.path, v)}
                            pending={pending(tr.path)}
                          />
                        </td>
                      )}
                      {trackCols.includes("artist") && <td className={`td text-zinc-400 break-words${phoneHide(TRACK_PHONE_CLS, "artist")}`}>{tr.artist}</td>}
                      {trackCols.includes("album") && <td className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, "album")}`}>{tr.album}</td>}
                      {trackCols.includes("year") && <td className={`td text-zinc-500${phoneHide(TRACK_PHONE_CLS, "year")}`} title={tr.tags.DATE ?? undefined}>{fmtDateCell(tr.tags.DATE, fullDates)}</td>}
                      {trackCols.includes("genre") && <td className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, "genre")}`}>{tr.tags.GENRE ?? "—"}</td>}
                      {trackCols.includes("media") && <td className={`td${phoneHide(TRACK_PHONE_CLS, "media")}`}><MediaChip media={tr.tags.MEDIA} /></td>}
                      {trackCols.includes("duration") && (
                        /* Duration only. A 64 px cell cannot hold the 70 px
                           star block beside a length, and a fixed-layout
                           table paints whatever does not fit over the next
                           column — which is how the stars ended up on top of
                           Bitrate. The rating rides in the Title cell, the
                           one cell that wraps to hold its marks (the album
                           page's tracklist has always drawn it there). */
                        <td className={`td text-zinc-500${phoneHide(TRACK_PHONE_CLS, "duration")}`}>
                          {fmtDuration(tr.tech.length)}
                        </td>
                      )}
                      {trackCols.includes("bitrate") && <td className={`td text-zinc-500${phoneHide(TRACK_PHONE_CLS, "bitrate")}`}>{fmtTech(tr.tech) || "—"}</td>}
                      {trackCols.includes("dr") && (
                        <td className={`td text-zinc-500 tabular-nums${phoneHide(TRACK_PHONE_CLS, "dr")}`} title={`Dynamic range${tr.tags["ALBUM DYNAMIC RANGE"] ? ` · album ${tr.tags["ALBUM DYNAMIC RANGE"]}` : ""}`}>
                          {tr.tags["DYNAMIC RANGE"] ?? "—"}
                        </td>
                      )}
                      {trackCols.includes("source") && <td className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, "source")}`}>{tr.tags.SOURCE ?? "—"}</td>}
                      {trackCols.includes("type") && (
                        <td className={`td text-zinc-500${phoneHide(TRACK_PHONE_CLS, "type")}`} title={tr.is_video ? "Music video" : "Audio track"}>
                          {tr.is_video ? (
                            <span className="inline-flex items-center gap-1"><FileVideo className="h-3.5 w-3.5" /> Video</span>
                          ) : "Audio"}
                        </td>
                      )}
                      {trackCols.includes("inst") && (
                        <td className={`td${phoneHide(TRACK_PHONE_CLS, "inst")}`}>
                          {tr.tags.INSTRUMENTAL === "1"
                            ? <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[10px]">INST</span>
                            : <span className="text-zinc-600">—</span>}
                        </td>
                      )}
                      {trackCols.includes("composer") && <td className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, "composer")}`} title="Composer">{tr.tags.COMPOSER ?? "—"}</td>}
                      {trackCols.includes("lyricist") && <td className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, "lyricist")}`} title="Lyricist">{tr.tags.LYRICIST ?? "—"}</td>}
                      {trackCols.includes("remixer") && <td className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, "remixer")}`} title="Remixer">{tr.tags.REMIXER ?? "—"}</td>}
                      {trackCustom.filter((c) => trackCols.includes(c.id)).map((c) => (
                        <td key={c.id} className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, c.id)}`} title={`Tag: ${c.tag}`}>
                          {customColValue(tr, c.tag) || "—"}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/** Where an added album is in its acquisition: the queue's own word for the
 *  stage and its own percentage while bytes are moving (lib/acquisition).
 *
 *  The Pending dot beside it says WHY a folder is empty; this says how far
 *  along the thing filling it is, so a library row answers "what is happening
 *  to this album" without opening the Soulseek page. Nothing is drawn for an
 *  album with no acquisition on its way — a settled library looks exactly as
 *  it did before this existed. */
function AcquisitionChip({ acq, className = "" }: { acq: Acquisition | null; className?: string }) {
  if (!acq) return null;
  const pct = acq.percent === null ? "" : ` · ${Math.round(acq.percent)}%`;
  return (
    <span
      className={`chip text-[9px] bg-sky-900/40 text-sky-300 border border-sky-800 shrink-0 ${className}`}
      title={`This album's acquisition: ${acq.label}${pct}`}
    >
      {acq.label}{pct}
    </span>
  );
}

/** One labelled block of the filter menu. The note rides under the group's
 *  label rather than in a tooltip: `RATED_NOTE` and the advisory ladder are the
 *  definitions of what the options below them mean, and a definition nobody can
 *  read without hovering is one the reader has to guess at. */
function FilterGroup({ label, note, children }: { label: string; note?: string; children: ReactNode }) {
  return (
    <div className="border-b border-border/60 last:border-b-0 py-1">
      <div className="px-2.5 pt-1 text-[10px] uppercase tracking-wider text-zinc-600">{label}</div>
      {note && <div className="px-2.5 pb-1 text-[10px] text-zinc-600 leading-snug">{note}</div>}
      {children}
    </div>
  );
}

/** One option: name, how many rows it would leave, and whether it is the one
 *  in force. Clicking never closes the menu — the facets compose, and a menu
 *  that closed on every pick would make the second one a reopen. */
function FilterRow({ label, hint, count, active, onClick }: {
  label: string; hint?: string; count: number; active: boolean; onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      title={hint}
      aria-pressed={active}
      className={`w-full text-left px-2.5 py-1.5 rounded-md text-xs flex items-center justify-between gap-3 ${
        active ? "bg-raise text-white" : "text-zinc-400 hover:text-white hover:bg-raise"
      }`}
    >
      <span className="truncate">{label}</span>
      <span className={`font-mono shrink-0 ${count === 0 ? "text-zinc-700" : "text-zinc-600"}`}>{count}</span>
    </button>
  );
}

function AlbumRowGroup({
  album,
  acq,
  expanded,
  onToggle,
  visibleCols,
  selected,
  onToggleSel,
  selTracks,
  onToggleTrack,
  removing,
  onRemove,
  onPlaylist,
  onTrackDetails,
  colSpan,
  fullDates,
  selectMode,
  trackCols,
  trackCustom = [],
  tagCols,
  trackWidths,
  onTrackWidth,
  onResetTrackWidths,
}: {
  album: FlatAlbum;
  expanded: boolean;
  onToggle: () => void;
  visibleCols: string[];
  selected: boolean;
  onToggleSel: () => void;
  selTracks: Set<string>;
  onToggleTrack: (p: string) => void;
  removing: boolean;
  onRemove: () => void;
  onPlaylist: () => void;
  onTrackDetails: (t: Track) => void;
  colSpan: number;
  fullDates: boolean;
  selectMode: boolean;
  trackCols: string[];
  /** Tag columns of the nested tracklist itself (shared with the album page
   *  through the same customs key — see the caller). */
  trackCustom?: CustomCol[];
  /** Tag columns added in the Columns menu — appended after the built-ins,
   *  in the same order as their headers. */
  tagCols: CustomCol[];
  trackWidths: Record<string, number>;
  onTrackWidth: (id: string, px: number) => void;
  onResetTrackWidths: () => void;
  /** Where this album is in its acquisition, when it is still arriving (see
   *  `AcquisitionChip`). */
  acq: Acquisition | null;
}) {
  // The rows this component renders are their own tree: same hooks as the
  // page, and react-query serves them from one GET /api/ratings per scope.
  const { data: ratingsData } = useRatings();
  const { setRating, pending } = useSetRating();
  const { data: albumRatingsData } = useRatings("album");
  const { setRating: setAlbumRating, pending: albumPending } = useSetRating("album");
  const ratings = ratingsData?.ratings;
  const albumRatings = albumRatingsData?.ratings;
  const navigate = useNavigate();
  const tracks = useMemo(() => [...(album.tracks ?? [])].sort(byDiscThenTrack), [album.tracks]);
  // The album-name cell IS the row title (AlbumRow renders it, with the link
  // and select-mode handling); the rest of the visible columns become cells.
  const showAlbumCol = visibleCols.includes("album");
  const cells: AlbumRowCell[] = [];
  if (visibleCols.includes("artist"))
    cells.push({ id: "artist", cls: `td text-zinc-400 break-words${phoneHide(ALBUM_PHONE_CLS, "artist")}`, node: album.artist });
  if (visibleCols.includes("year"))
    cells.push({
      id: "year", cls: `td text-zinc-500${phoneHide(ALBUM_PHONE_CLS, "year")}`,
      title: album.meta?.ORIGINALDATE ?? album.meta?.DATE ?? undefined,
      node: fmtDateCell(album.meta?.ORIGINALDATE || album.meta?.DATE, fullDates),
    });
  if (visibleCols.includes("tracks"))
    cells.push({ id: "tracks", cls: `td text-zinc-500${phoneHide(ALBUM_PHONE_CLS, "tracks")}`, node: album.track_count });
  if (visibleCols.includes("rating"))
    cells.push({
      id: "rating",
      cls: `td${phoneHide(ALBUM_PHONE_CLS, "rating")}`,
      node: (
        <StarRating
          size="sm"
          label="Album rating"
          hint={`Your rating for the album. ${FOLDER_RATING_NOTE}`}
          value={ratingOf(albumRatings, album.path)}
          onChange={(v) => setAlbumRating(album.path, v)}
          pending={albumPending(album.path)}
        />
      ),
    });
  if (visibleCols.includes("grade"))
    cells.push({
      id: "grade",
      cls: `td${phoneHide(ALBUM_PHONE_CLS, "grade")}`,
      node: <GradeBadge pass={!!album.pass && !auditFails(album.audit_summary)} score={album.grade_pct} />,
    });
  if (visibleCols.includes("media"))
    cells.push({
      id: "media", cls: `td${phoneHide(ALBUM_PHONE_CLS, "media")}`,
      node: <MediaChip media={album.media} />,
    });
  if (visibleCols.includes("dr"))
    cells.push({
      id: "dr", cls: `td text-zinc-500 tabular-nums${phoneHide(ALBUM_PHONE_CLS, "dr")}`, title: "Album dynamic range",
      node: album.meta?.["ALBUM DYNAMIC RANGE"] ?? "—",
    });
  if (visibleCols.includes("source"))
    cells.push({ id: "source", cls: `td text-zinc-500 break-words${phoneHide(ALBUM_PHONE_CLS, "source")}`, node: album.source_summary ?? "—" });
  if (visibleCols.includes("videos"))
    cells.push({
      id: "videos", cls: `td text-zinc-500 tabular-nums${phoneHide(ALBUM_PHONE_CLS, "videos")}`, title: "Music videos in this album",
      node: album.video_count || "—",
    });
  if (visibleCols.includes("inst"))
    cells.push({
      id: "inst", cls: `td text-zinc-500 tabular-nums${phoneHide(ALBUM_PHONE_CLS, "inst")}`, title: "Instrumental tracks in this album",
      node: album.inst_count || "—",
    });
  // Tag columns last — the header renders them in this same order.
  for (const c of tagCols) {
    if (!visibleCols.includes(c.id)) continue;
    cells.push({
      id: c.id, cls: `td text-zinc-500 break-words${phoneHide(ALBUM_PHONE_CLS, c.id)}`, title: `Tag: ${c.tag}`,
      node: customColValue({ tags: album.meta }, c.tag) || "—",
    });
  }

  return (
    <>
      <AlbumRow
        title={showAlbumCol ? (album.meta?.ALBUM ?? album.path.split("/").pop()) : null}
        titleHref={albumRef(album)}
        titleExtra={
          <>
            {showAlbumCol ? <AdvisoryMark value={album.meta?.ITUNESADVISORY ?? album.meta?.ALBUMITUNESADVISORY} /> : null}
            {/* the same marker the compact rows and the cards carry — the
                albums table is one more album-shaped surface */}
            <PendingMark album={album} />
            <AcquisitionChip acq={acq} />
          </>
        }
        coverPath={album.path}
        coverFile={album.cover_file}
        coverTitle={selectMode ? "Click to select" : "Open album page"}
        cells={cells}
        actions={
          <>
            {/* Row actions are always visible on touch, so they carry a 32 px
                tap target on a phone and the library's compact size from `md`. */}
            <button className="btn-ghost !px-1.5 !py-2 md:!py-1" title="Add to playlist" onClick={onPlaylist}>
              <ListPlus className="h-3.5 w-3.5" />
            </button>
            <button className="btn-danger !px-1.5 !py-2 md:!py-1" title="Remove album (to trash)" disabled={removing} onClick={onRemove}>
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </>
        }
        onRowClick={onToggle}
        selected={selected}
        selectMode={selectMode}
        onToggleSel={onToggleSel}
        showExpand
        expanded={expanded}
        onToggle={onToggle}
        colSpan={colSpan}
        expandedContent={
          /* Its own scroll wrapper: this nested table is what overflows first
             on a phone, and the outer wrapper cannot scroll for it. Its floor
             is the album tracklist's own (shared columns, shared floor). */
          <div className="overflow-x-auto">
            <table className={`w-full ${ALBUM_TRACK_MIN_W}`}>
              <thead className="border-b border-border">
                <tr>
                  {selectMode && <th className="th w-8"></th>}
                  {[...ALBUM_TRACK_COLS, ...customCols(trackCustom, "tags")].filter((c) => trackCols.includes(c.id)).map((c) =>
                    c.id === "cover" ? (
                      <th key={c.id} className={`th relative ${ALBUM_TRACK_COL_W[c.id] ?? TAG_COL_W}${phoneHide(TRACK_PHONE_CLS, c.id)}`} title="Cover art">
                        <span className="sr-only">Cover</span>
                      </th>
                    ) : (
                      <th key={c.id} className={`th relative ${ALBUM_TRACK_COL_W[c.id] ?? TAG_COL_W}${phoneHide(TRACK_PHONE_CLS, c.id)}`} style={trackWidths[c.id] ? { width: trackWidths[c.id] } : undefined}>
                        {c.label}
                        <ColumnResizer width={trackWidths[c.id]} onDrag={(w) => onTrackWidth(c.id, w)} onReset={onResetTrackWidths} />
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {(() => {
                  const groups = groupByDisc(tracks);
                  return groups.map((g) => (
                    <Fragment key={g.disc ?? 0}>
                      {g.tracks.map((t) => (
                        <tr
                          key={t.path}
                          className={`table-row group cursor-pointer ${selTracks.has(t.path) ? "bg-accent/15" : ""}`}
                          title={selectMode ? "Click to select" : "Click to play"}
                          onClick={selectMode ? () => onToggleTrack(t.path) : () =>
                            useStore.getState().playNow(
                              tracks.map((x) => ({
                                path: x.path, file: x.file, albumPath: album.path,
                                artist: album.artist, album: album.meta?.ALBUM ?? undefined, title: x.tags.TITLE || undefined,
                                coverFile: x.cover_file ?? null, albumCover: album.cover_file ?? null,
                                advisory: x.tags.ITUNESADVISORY ?? null,
                              })),
                              tracks.findIndex((x) => x.path === t.path)
                            )
                          }
                        >
                          {selectMode && (
                            <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                              <input type="checkbox" className="" checked={selTracks.has(t.path)} onChange={() => onToggleTrack(t.path)} />
                            </td>
                          )}
                          {trackCols.includes("num") && (
                            <td className={`td text-zinc-600 tabular-nums cell-nowrap${phoneHide(TRACK_PHONE_CLS, "num")}`}>
                              {groups.length > 1 ? `${g.disc}-${t.tracknumber ?? t.tags.TRACKNUMBER ?? "?"}` : t.tracknumber ?? t.tags.TRACKNUMBER ?? "—"}
                            </td>
                          )}
                          {trackCols.includes("cover") && (
                            <td className="td cell-cover pr-0">
                              <TrackCover
                                albumPath={album.path}
                                trackCover={t.cover_file}
                                wrapperClass="h-8 w-8 rounded bg-raise overflow-hidden shrink-0"
                              />
                            </td>
                          )}
                          {trackCols.includes("title") && (
                            <td className="td">
                              <TrackTitleCell
                                trailing={
                                  <>
                                    <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
                                      <FavHeart kind="track" id={t.path} mbid={t.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" revealOnHover />
                                    </span>
                                    <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                                      <TrackActionsMenu path={t.path} releaseMbid={t.tags.MUSICBRAINZ_ALBUMID} />
                                    </span>
                                    <button
                                      className="text-zinc-500 hover:text-accent-soft shrink-0"
                                      title="Grading & audit details"
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        onTrackDetails(t);
                                      }}
                                    >
                                      <InfoIcon className="h-3.5 w-3.5" />
                                    </button>
                                    {/* the rating in the row's fixed slot —
                                        the same x on every track of the album,
                                        and out of the 80 px Dur column beside
                                        it, which cannot hold both */}
                                    <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
                                      <StarRating size="sm" value={ratingOf(ratings, t.path)} onChange={(v) => setRating(t.path, v)} pending={pending(t.path)} />
                                    </span>
                                  </>
                                }
                              >
                                <Link
                                  to={trackRef(t)}
                                  className="hover:text-accent-soft break-words min-w-0"
                                  title="Click to play · Ctrl-click to open track page"
                                  onClick={(e) => entityLinkClick(e, () => navigate(trackRef(t)))}
                                >
                                  {t.tags.TITLE ?? t.file}
                                </Link>
                                <AdvisoryMark value={t.tags.ITUNESADVISORY} />
                                <LockedChip path={t.path} />
                                {!!t.issues?.length && (
                                  <button
                                    className="text-[9px] text-red-400/70 shrink-0 hover:text-red-300"
                                    title={`${t.issues.join("\n")}\nClick for details`}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      onTrackDetails(t);
                                    }}
                                  >
                                    {t.issues.length}✗
                                  </button>
                                )}
                                <GradeBadge pass={!!t.grade_pass && !auditFails(t.audit)} size="sm" />
                                <CachedMark path={t.path} />
                                {t.is_video && <span title="Music video" className="shrink-0 inline-flex"><FileVideo className="h-3.5 w-3.5 text-zinc-500" /></span>}
                                {t.tags.INSTRUMENTAL === "1" && (
                                  <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[10px] shrink-0">INST</span>
                                )}
                              </TrackTitleCell>
                            </td>
                          )}
                          {trackCols.includes("genre") && <td className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, "genre")}`}>{t.tags.GENRE ?? "—"}</td>}
                          {trackCols.includes("dur") && (
                            /* Folds with its siblings on a phone: `dur` is the
                               id this table's header uses for the same column
                               the Tracks view calls `duration`. */
                            <td className={`td text-zinc-500${phoneHide(TRACK_PHONE_CLS, "dur")}`}>
                              {fmtDuration(t.tech.length)}
                            </td>
                          )}
                          {trackCols.includes("bitrate") && (
                            <td className={`td text-zinc-500${phoneHide(TRACK_PHONE_CLS, "bitrate")}`}>{fmtTech(t.tech) || "—"}</td>
                          )}
                          {trackCols.includes("dr") && (
                            <td className={`td text-zinc-500 tabular-nums${phoneHide(TRACK_PHONE_CLS, "dr")}`} title={`Dynamic range${t.tags["ALBUM DYNAMIC RANGE"] ? ` · album ${t.tags["ALBUM DYNAMIC RANGE"]}` : ""}`}>
                              {t.tags["DYNAMIC RANGE"] ?? "—"}
                            </td>
                          )}
                          {/* the tracklist's own tag columns, after the
                              built-ins (same order as their headers) */}
                          {trackCustom.map((c) =>
                            trackCols.includes(c.id) ? (
                              <td key={c.id} className={`td text-zinc-500 break-words${phoneHide(TRACK_PHONE_CLS, c.id)}`} title={c.label}>
                                {customColValue(t, c.tag) || "—"}
                              </td>
                            ) : null
                          )}
                        </tr>
                      ))}
                    </Fragment>
                  ));
                })()}
              </tbody>
            </table>
          </div>
        }
      />
    </>
  );
}

function ScriptsDropdown({ onRun, runAllIds }: { onRun: (ids: number[], force?: boolean) => void; runAllIds: number[] }) {
  const [open, setOpen] = useState(false);
  const items: { ids: number[]; label: string; force?: boolean }[] = [
    { ids: runAllIds, label: "Run all" },
    { ids: runAllIds, label: "Run all (force)", force: true },
    ...SCRIPTS,
  ];
  return (
    <div className="relative">
      <button className="btn-ghost !py-1 text-xs tap" onClick={() => setOpen(!open)}>
        <Wand2 className="h-3.5 w-3.5" /> Scripts
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 bg-zinc-950 border border-border rounded-lg p-1.5 w-44 shadow-2xl">
            {items.map((s) => (
              <button
                key={s.label}
                className={`w-full text-left px-2.5 py-1.5 text-xs rounded hover:bg-panel ${s.force ? "text-accent-soft" : "text-zinc-300"} hover:text-white`}
                onClick={() => {
                  setOpen(false);
                  onRun(s.ids, s.force);
                }}
              >
                {s.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function useLocalPref(key: string, initial: boolean): [boolean, (v: boolean) => void] {
  const storageKey = `mlo-pref-${key}`;
  const [value, setValue] = useState<boolean>(() => {
    try {
      return localStorage.getItem(storageKey) === "1";
    } catch {
      return initial;
    }
  });
  const set = (v: boolean) => {
    setValue(v);
    try {
      localStorage.setItem(storageKey, v ? "1" : "0");
    } catch {
      /* ignore */
    }
  };
  return [value, set];
}
