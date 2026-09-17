import { Fragment, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowDownUp, BarChart3, ChevronDown, ChevronRight, CloudDownload,
  FileVideo, FolderSync, Info as InfoIcon, Layers, Library, ListChecks,
  ListFilter, ListPlus, Play, Tag, Trash2, Wand2,
} from "lucide-react";
import { api } from "../api";
import { SCRIPTS, DEFAULT_RUN_ALL, isScriptId } from "../lib/scripts";
import { toast, useStore } from "../store";
import {
  sortRows, SortHeader, groupByDisc, byDiscThenTrack, type SortState,
} from "../lib/sort.tsx";
import {
  ColumnResizer, ColumnsMenu, useColumnPrefs, useColumnWidths,
  ALBUM_TRACK_COLS, ALBUM_TRACK_COL_W, type Col,
} from "../lib/columns";
import { gradeSliver, statusFor, auditFails } from "../lib/status";
import { invalidateLibrary } from "../lib/invalidate";
import { albumRef, trackRef, artistRef, entityLinkClick } from "../lib/refs";
import { fmtTech, fmtDuration, fmtDateCell, originalYear, GRID_SIZE_MIN } from "../lib/fmt";
import { EmptyState, GradeBadge, MediaChip, AdvisoryMark, PageLoading } from "../components/Badges";
import { forceDict, loadForceSel } from "../lib/force";
import Segmented from "../components/Segmented";
import PageHeader from "../components/PageHeader";
import CoverImg, { TrackCover } from "../components/CoverImg";
import FavHeart from "../components/FavHeart";
import AlbumCard from "../components/AlbumCard";
import AlbumRow, { type AlbumRowCell } from "../components/AlbumRow";
import StatsPanel from "../components/StatsPanel";
import TrackDetails from "../components/TrackDetails";
import BulkTagsDialog from "../components/BulkTagsDialog";
import type { Album, Artist, Track } from "../types";

type View = "grid" | "compact" | "albums" | "artists" | "tracks";

type Preset =
  | "all"
  | "failing"
  | "cd"
  | "digital"
  | "explicit"
  | "instrumental"
  | "missingLyrics"
  | "videos";

const PRESETS: { id: Preset; label: string }[] = [
  { id: "all", label: "All" },
  { id: "failing", label: "Failing" },
  { id: "cd", label: "CD rips" },
  { id: "digital", label: "Digital" },
  { id: "explicit", label: "Explicit" },
  { id: "instrumental", label: "Instrumental" },
  { id: "videos", label: "Music videos" },
  { id: "missingLyrics", label: "No lyrics" },
];

const VIEW_TABS: { id: View; label: string }[] = [
  { id: "grid", label: "Grid" },
  { id: "compact", label: "Compact" },
  { id: "albums", label: "Albums" },
  { id: "artists", label: "Artists" },
  { id: "tracks", label: "Tracks" },
];

const ALBUM_SORTS = [
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

/** Column widths for the fixed table layout: percentages compress with
 * the window; "album"/"title" has no width and absorbs whatever is left. */
const ALBUM_COL_W: Record<string, string> = {
  album: "w-auto",
  artist: "w-[16%]",
  year: "w-[7%]",
  tracks: "w-[7%]",
  grade: "w-[10%]",
  media: "w-[10%]",
  dr: "w-[6%]",
  source: "w-[13%]",
  videos: "w-[7%]",
  inst: "w-[7%]",
};

const ALBUM_COLS: Col[] = [
  { id: "album", label: "Album", sortKey: "meta.ALBUM" },
  { id: "artist", label: "Artist", sortKey: "artist" },
  { id: "year", label: "Year", sortKey: "meta.DATE" },
  { id: "tracks", label: "Tracks", sortKey: "track_count" },
  { id: "grade", label: "Grade", sortKey: "grade_pct" },
  { id: "media", label: "Media", sortKey: "media" },
  { id: "dr", label: "DR", sortKey: "meta.ALBUM DYNAMIC RANGE" },
  { id: "source", label: "Source", sortKey: "source_summary" },
  { id: "videos", label: "Videos", sortKey: "video_count" },
  { id: "inst", label: "INST", sortKey: "inst_count" },
];

const ARTIST_COL_W: Record<string, string> = {
  albums: "w-[12%]",
  tracks: "w-[12%]",
  checks: "w-[12%]",
  grade: "w-[16%]",
};

const ARTIST_COLS: Col[] = [
  { id: "albums", label: "Albums", sortKey: "aggregate.album_count" },
  { id: "tracks", label: "Tracks", sortKey: "aggregate.track_count" },
  { id: "checks", label: "Checks", sortKey: "aggregate.grade_pct" },
  { id: "grade", label: "Grade", sortKey: "aggregate.grade_pct" },
];

const TRACK_COL_W: Record<string, string> = {
  num: "w-16",
  cover: "w-[52px]",
  title: "w-auto",
  artist: "w-[11%]",
  album: "w-[11%]",
  year: "w-[6%]",
  genre: "w-[10%]",
  media: "w-[8%]",
  duration: "w-[6%]",
  bitrate: "w-[9%]",
  dr: "w-[5%]",
  source: "w-[9%]",
  type: "w-[6%]",
  inst: "w-[6%]",
  composer: "w-[10%]",
  lyricist: "w-[10%]",
  remixer: "w-[9%]",
};

const TRACK_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "tracknumber" },
  { id: "cover", label: "", sortKey: "" },
  { id: "title", label: "Title", sortKey: "tags.TITLE" },
  { id: "artist", label: "Artist", sortKey: "artist" },
  { id: "album", label: "Album", sortKey: "album" },
  { id: "year", label: "Year", sortKey: "tags.DATE" },
  { id: "genre", label: "Genre", sortKey: "tags.GENRE" },
  { id: "media", label: "Media", sortKey: "tags.MEDIA" },
  { id: "duration", label: "Duration", sortKey: "tech.length" },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate" },
  // ReplayGain deliberately has NO column: it is playback metadata — the
  // player applies it to keep loudness even between tracks. Only Dynamic
  // Range is shown.
  { id: "dr", label: "DR", sortKey: "tags.DYNAMIC RANGE" },
  { id: "source", label: "Source", sortKey: "tags.SOURCE" },
  { id: "type", label: "Type", sortKey: "is_video" },
  { id: "inst", label: "INST", sortKey: "tags.INSTRUMENTAL" },
  { id: "composer", label: "Composer", sortKey: "tags.COMPOSER", defHidden: true },
  { id: "lyricist", label: "Lyricist", sortKey: "tags.LYRICIST", defHidden: true },
  { id: "remixer", label: "Remixer", sortKey: "tags.REMIXER", defHidden: true },
];

interface FlatAlbum extends Album {
  artist: string;
  video_count: number;
  inst_count: number;
}

interface FlatTrack extends Track {
  artist: string;
  album: string;
  albumCover?: string | null;
  albumPath: string;
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
  const { data: lib, isLoading, error } = useQuery({ queryKey: ["library"], queryFn: api.library });
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const runAllIds = Array.isArray(config?.run_all_order) && config.run_all_order.length
    ? config.run_all_order.filter((n: number) => isScriptId(n))
    : DEFAULT_RUN_ALL;
  const qc = useQueryClient();
  const navigate = useNavigate();
  // Per-slice selectors: a bare useStore() subscribes this page to every
  // store write (progress ticks, volume, queue) and re-renders the tables.
  const query = useStore((s) => s.query);
  const selection = useStore((s) => s.selection);
  const setSelection = useStore((s) => s.setSelection);
  const toggleTrack = useStore((s) => s.toggleTrack);
  const toggleAlbum = useStore((s) => s.toggleAlbum);
  const toggleArtist = useStore((s) => s.toggleArtist);
  const clearSelection = useStore((s) => s.clearSelection);
  const playNow = useStore((s) => s.playNow);
  const [view, setView] = useState<View>(() => (localStorage.getItem("mlo.defaultView.v2") as View) ?? "grid");
  // checkboxes (and the batch toolbar they feed) only exist in select mode
  const [selectMode, setSelectMode] = useState(false);
  const toggleSelectMode = () => {
    setSelectMode((v) => {
      if (v) clearSelection();
      return !v;
    });
  };
  const [preset, setPreset] = useState<Preset>("all");
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
  const [gridSize, setGridSize] = useState<"s" | "m" | "l">(() => {
    const v = localStorage.getItem("mlo.gridSize");
    return v === "s" || v === "l" ? v : "m";
  });
  const [statsOpen, setStatsOpen] = useState(false);
  const [detailTrack, setDetailTrack] = useState<{ track: Track; albumPath: string } | null>(null);
  const [bulkTagsOpen, setBulkTagsOpen] = useState(false);

  const [fullDates, setFullDates] = useLocalPref("full-dates", false);
  const [albumCols, toggleAlbumCol] = useColumnPrefs("albums", ALBUM_COLS);
  const [artistCols, toggleArtistCol] = useColumnPrefs("artists", ARTIST_COLS);
  const [trackCols, toggleTrackCol] = useColumnPrefs("tracks", TRACK_COLS);
  // Album tracklists (the expanded album rows here share these prefs — and
  // their widths — with the album page, since they are the same table).
  const [alTrackCols, toggleAlTrackCol] = useColumnPrefs("album-tracks", ALBUM_TRACK_COLS);
  const [alTrackW, setAlTrackW, resetAlTrackW] = useColumnWidths("album-tracks");
  // Drag-resized column widths, persisted per view ("Reset" in the Columns
  // menu — or double-click a handle — restores the fluid defaults).
  const [albumW, setAlbumW, resetAlbumW] = useColumnWidths("albums");
  const [artistW, setArtistW, resetArtistW] = useColumnWidths("artists");
  const [trackW, setTrackW, resetTrackW] = useColumnWidths("tracks");

  const flat = useMemo(() => {
    const albums: FlatAlbum[] = [];
    const tracks: FlatTrack[] = [];
    for (const a of lib?.artists ?? [])
      for (const al of a.albums) {
        // Prefer the tag-derived album artist (ALBUMARTIST/ARTIST); the
        // artist folder name is only a fallback (it carries the MBID suffix).
        const artistName = al.album_artist || a.name;
        albums.push({
          ...al,
          artist: artistName,
          video_count: (al.tracks ?? []).filter((t) => t.is_video).length,
          inst_count: (al.tracks ?? []).filter((t) => t.tags.INSTRUMENTAL === "1").length,
        });
        for (const t of al.tracks) tracks.push({ ...t, artist: artistName, album: al.meta?.ALBUM ?? al.path.split("/").pop() ?? "", albumCover: al.cover_file ?? null, albumPath: al.path });
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
      case "explicit": return t.tags.ITUNESADVISORY === "1";
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
      case "explicit":
      case "instrumental":
      case "videos":
      case "missingLyrics": return (al.tracks ?? []).some((t) => trackPresetOK(t, preset));
    }
  };
  const presetCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const { id } of PRESETS) out[id] = flat.albums.filter((al) => albumPresetOK(al, id)).length;
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flat]);

  const filtered = useMemo(() => {
    if (!lib) return { artists: [] as Artist[], albums: [] as FlatAlbum[], tracks: [] as FlatTrack[] };
    const terms = parseQueryTerms(query);
    const words = terms.words;
    const wordsMatch = (hay: string) => words.every((w) => hay.toLowerCase().includes(w));

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

    // plain words search EVERY tag value plus the flattened names
    const trackHay = (t: Track & { artist?: string; album?: string }) =>
      [t.artist, t.album, t.file, ...Object.values(t.tags ?? {}).filter(Boolean).map(String)].join(" ");
    const trOK = (t: Track) => trackPresetOK(t, preset) && trackTagOK(t) && wordsMatch(trackHay(t));

    const alOK = (al: Album) => albumPresetOK(al, preset);
    const alTagOK = (al: Album) =>
      terms.tags.every(({ key, value }) =>
        tagTermOK((al.meta ?? {}) as Record<string, unknown>, key, value) ||
        (al.tracks ?? []).some((t) => tagTermOK(t.tags as Record<string, unknown>, key, value)));
    const alSearch = (al: Album, artist: string) =>
      wordsMatch([artist, al.meta?.ALBUM, al.meta?.DATE, al.meta?.ARTIST, al.meta?.LABEL, al.meta?.CATALOGNUMBER, ...(al.tracks ?? []).map(trackHay)].join(" ")) &&
      alTagOK(al);

    const artists: Artist[] = lib.artists
      .map((a) => ({ ...a, albums: a.albums.filter((al) => alOK(al) && alSearch(al, a.name)) }))
      .filter((a) => a.albums.length);

    const albums = flat.albums.filter((al) => alOK(al) && alSearch(al, al.artist));
    const tracks = flat.tracks.filter(trOK);
    return { artists, albums, tracks };
  }, [lib, query, preset, flat]);

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

  const playSelection = () => {
    const out: { path: string; file: string; albumPath: string; artist?: string; album?: string; title?: string; coverFile?: string | null; albumCover?: string | null }[] = [];
    for (const al of sortedAlbums)
      if (selection.albums.includes(al.path))
        for (const t of al.tracks) out.push({ path: t.path, file: t.file, albumPath: al.path, artist: al.artist, album: al.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined, coverFile: t.cover_file ?? null, albumCover: al.cover_file ?? null });
    for (const a of sortedArtists)
      if (selection.artists.includes(a.path))
        for (const al of a.albums)
          for (const t of al.tracks) out.push({ path: t.path, file: t.file, albumPath: al.path, artist: al.album_artist || a.name, album: al.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined, coverFile: t.cover_file ?? null, albumCover: al.cover_file ?? null });
    for (const tr of sortedTracks)
      if (selection.tracks.includes(tr.path))
        out.push({ path: tr.path, file: tr.file, albumPath: tr.path.split("/").slice(0, -1).join("/"), artist: tr.artist, album: tr.album, title: tr.tags.TITLE || undefined, coverFile: tr.cover_file ?? null, albumCover: tr.albumCover ?? null });
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

  // Sorting is memoized so typing in the search box / toggling selection
  // doesn't re-sort the whole library on every keystroke.
  const sortedAlbums = useMemo(() => sortRows(filtered.albums, albumSort), [filtered.albums, albumSort]);
  const sortedArtists = useMemo(() => sortRows(filtered.artists, artistSort), [filtered.artists, artistSort]);
  const sortedTracks = useMemo(() => sortRows(filtered.tracks, trackSort), [filtered.tracks, trackSort]);

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

  const pickGridSize = (s: "s" | "m" | "l") => {
    setGridSize(s);
    try {
      localStorage.setItem("mlo.gridSize", s);
    } catch {
      /* ignore */
    }
  };

  if (error) return <EmptyState title="Backend unreachable" hint={String(error)} />;
  if (isLoading || !lib) return <PageLoading label="Scanning library…" />;

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

  const allAlbumsSelected = sortedAlbums.length > 0 && sortedAlbums.every((a) => selection.albums.includes(a.path));
  const allArtistsSelected = sortedArtists.length > 0 && sortedArtists.every((a) => selection.artists.includes(a.path));
  const allTracksSelected = sortedTracks.length > 0 && sortedTracks.every((t) => selection.tracks.includes(t.path));

  return (
    <div className="p-6 space-y-5">
      {/* toolbar rides in the header: controls left, stats/select/counts right */}
      <PageHeader icon={Library} title="Library">
      {/* toolbar — every control on ONE line (wrapped as a unit when the
          window is narrow): view tabs, sort, grid size, group-by, columns,
          quick filter — then stats/select and the counts on the right. */}
      <div className="flex items-center gap-2 flex-wrap">
        <Segmented value={view} onChange={setView} options={VIEW_TABS} />

        {(view === "albums" || view === "compact" || view === "grid") && (
          <div className="relative">
            <button
              className={`btn-ghost !py-1.5 text-xs ${sortOpen ? "!text-white !bg-raise" : ""}`}
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
          <div className="flex rounded-md border border-border overflow-hidden" title="Cover size">
            {(["s", "m", "l"] as const).map((s) => (
              <button
                key={s}
                onClick={() => pickGridSize(s)}
                className={`px-2.5 py-1.5 text-xs font-medium uppercase transition-colors ${
                  gridSize === s ? "bg-accent on-accent" : "bg-panel text-zinc-400 hover:text-white"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        )}

        {(view === "albums" || view === "grid") && (
          <button
            className={`btn-ghost !py-1.5 text-xs ${groupByArtist ? "!text-accent !border-accent/50" : ""}`}
            onClick={() => setGroupByArtist(!groupByArtist)}
            title="Group albums under artist headers"
          >
            <Layers className="h-3.5 w-3.5" /> Group by artist
          </button>
        )}

        {view !== "compact" && view !== "grid" && (
          <ColumnsMenu
            cols={view === "albums" ? ALBUM_COLS : view === "artists" ? ARTIST_COLS : TRACK_COLS}
            visible={view === "albums" ? albumCols : view === "artists" ? artistCols : trackCols}
            onToggle={view === "albums" ? toggleAlbumCol : view === "artists" ? toggleArtistCol : toggleTrackCol}
            fullDates={fullDates}
            onFullDates={setFullDates}
            onResetWidths={view === "albums" ? () => { resetAlbumW(); resetAlTrackW(); } : view === "artists" ? resetArtistW : resetTrackW}
            hasCustomWidths={
              Object.keys(view === "albums" ? albumW : view === "artists" ? artistW : trackW).length > 0 ||
              (view === "albums" && Object.keys(alTrackW).length > 0)
            }
            extraCols={view === "albums" ? ALBUM_TRACK_COLS : undefined}
            extraVisible={view === "albums" ? alTrackCols : undefined}
            onExtraToggle={view === "albums" ? toggleAlTrackCol : undefined}
          />
        )}

        {/* quick filter lives on the same line as the view options */}
        <div className="relative">
          <button
            className={`btn-ghost !py-1.5 text-xs ${filterOpen ? "!text-white !bg-raise" : ""}`}
            onClick={() => setFilterOpen(!filterOpen)}
            title="Filter the library"
          >
            <ListFilter className="h-3.5 w-3.5" />
            {PRESETS.find((p) => p.id === preset)?.label}
            <span className="text-zinc-600 font-mono">{presetCounts[preset] ?? ""}</span>
          </button>
          {filterOpen && (
            <>
              <div className="fixed inset-0 z-30" onClick={() => setFilterOpen(false)} />
              <div className="absolute left-0 top-full mt-1 z-40 w-52 rounded-lg border border-border bg-zinc-950 shadow-2xl p-1.5">
                {PRESETS.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => {
                      setPreset(p.id);
                      setFilterOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-md text-xs flex items-center justify-between gap-3 ${
                      preset === p.id ? "bg-raise text-white" : "text-zinc-400 hover:text-white hover:bg-raise"
                    }`}
                  >
                    <span>{p.label}</span>
                    <span className="text-zinc-600 font-mono">{presetCounts[p.id] ?? 0}</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            className="btn-ghost !py-1.5 text-xs"
            onClick={() => setStatsOpen(true)}
            title={selectionCount ? "Statistics for the current selection" : "Library-wide statistics"}
          >
            <BarChart3 className="h-3.5 w-3.5" /> Stats
          </button>
          <button
            className={`btn-ghost !py-1.5 text-xs ${selectMode ? "!text-accent !border-accent/50" : ""}`}
            onClick={toggleSelectMode}
            title="Select mode — show checkboxes for batch actions"
          >
            <ListChecks className="h-3.5 w-3.5" /> Select
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
            <button className="btn-primary !py-1 text-xs" onClick={playSelection}>
              <Play className="h-3.5 w-3.5" /> Play
            </button>
            <button className="btn-ghost !py-1 text-xs" onClick={() => addToPlaylist([...selTracks])}>
              <ListPlus className="h-3.5 w-3.5" /> Playlist
            </button>
            {(selection.albums.length > 0 || selection.artists.length > 0) && (
              <button
                className="btn-danger !py-1 text-xs"
                onClick={() => removeAlbums(selectionAlbumDirs)}
                disabled={busy || removing === "batch" || !selectionAlbumDirs.length}
                title="Move selected albums to trash"
              >
                <Trash2 className="h-3.5 w-3.5" /> Remove
              </button>
            )}
            <ScriptsDropdown onRun={runScriptsOnSelection} runAllIds={runAllIds} />
            <button
              className="btn-ghost !py-1 text-xs"
              onClick={downloadLyricsSelection}
              disabled={lyricsBusy}
              title="Auto-import missing lyrics for the selection through the provider chain (skips instrumentals)"
            >
              <CloudDownload className="h-3.5 w-3.5" /> {lyricsBusy ? "Fetching…" : "Lyrics"}
            </button>
            <button
              className="btn-ghost !py-1 text-xs"
              onClick={() => setBulkTagsOpen(true)}
              disabled={!selTracks.size}
              title="Bulk remove or set tags on the selected tracks"
            >
              <Tag className="h-3.5 w-3.5" /> Tags
            </button>
            <button
              className="btn-ghost !py-1 text-xs"
              onClick={organizeSelection}
              disabled={busy || removing === "batch" || !selectionAlbumDirs.length}
              title="Apply the naming script from Settings"
            >
              <FolderSync className="h-3.5 w-3.5" /> {busy ? "Organizing…" : "Organize"}
            </button>
            <button className="btn-ghost !py-1 text-xs" onClick={clearSelection}>
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
        <TrackDetails track={detailTrack.track} albumPath={detailTrack.albumPath} onClose={() => setDetailTrack(null)} />
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
                      wrapperClass="h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0"
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
                  <span className="text-[10px] text-zinc-600 shrink-0 w-8 text-right">{al.track_count}t</span>
                  <div className="opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 flex gap-1 shrink-0 transition-opacity" onClick={(e) => e.stopPropagation()}>
                    <button className="btn-ghost !px-1.5 !py-0.5" title={isExp ? "Collapse" : "Show tracks"}>
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
                              tracks.map((x) => ({ path: x.path, file: x.file, albumPath: al.path, artist: al.artist, album: al.meta?.ALBUM ?? undefined, title: x.tags.TITLE || undefined, coverFile: x.cover_file ?? null, albumCover: al.cover_file ?? null })),
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
                            wrapperClass="h-8 w-8 rounded bg-raise border border-border overflow-hidden shrink-0"
                          />
                          <Link to={trackRef(t)} className="break-words hover:text-accent-soft flex-1 min-w-0"
                            title="Click to play · Ctrl-click to open track page"
                            onClick={(e) => entityLinkClick(e, () => navigate(trackRef(t)))}
                          >
                            {t.tags.TITLE ?? t.file}
                          </Link>
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
                          <GradeBadge pass={!!t.grade_pass && !auditFails(t.audit)} audit={t.audit} size="sm" />
                          <AdvisoryMark value={t.tags.ITUNESADVISORY} />
                          <span className="row-hover shrink-0"><FavHeart kind="track" id={t.path} mbid={t.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" /></span>
                          {t.tags.INSTRUMENTAL === "1" && (
                            <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[9px] shrink-0">INST</span>
                          )}
                          <span className="text-[10px] text-zinc-600 font-mono w-10 text-right shrink-0 cell-nowrap">{fmtDuration(t.tech.length)}</span>
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
            <table className="w-full text-sm">
              <thead className="border-b border-border">
                <tr>
                  {selectMode && (
                    <th className="th w-8">
                      <input type="checkbox" className="" checked={allAlbumsSelected}
                        onChange={() => setSelection({ albums: allAlbumsSelected ? [] : sortedAlbums.map((a) => a.path) })} />
                    </th>
                  )}
                  <th className="th w-10"></th>
                  <th className="th w-14"></th>
                  {ALBUM_COLS.filter((c) => albumCols.includes(c.id)).map((c) => (
                    <SortHeader key={c.id} label={c.label} sort={albumSort} sortKey={c.sortKey} onSort={setAlbumSort}
                      className={`relative ${ALBUM_COL_W[c.id] ?? ""}`}
                      style={albumW[c.id] ? { width: albumW[c.id] } : undefined} >
                      <ColumnResizer width={albumW[c.id]} onDrag={(w) => setAlbumW(c.id, w)} onReset={() => resetAlbumW()} />
                    </SortHeader>
                  ))}
                  <th className="th w-24 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="stagger">
                {(groupByArtist ? albumRows : sortedAlbums.map((al) => ({ kind: "album" as const, album: al }))).map((row) =>
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
            <table className="w-full text-sm">
              <thead className="border-b border-border">
                <tr>
                  {selectMode && (
                    <th className="th w-8">
                      <input type="checkbox" className="" checked={allArtistsSelected}
                        onChange={() => setSelection({ artists: allArtistsSelected ? [] : sortedArtists.map((a) => a.path) })} />
                    </th>
                  )}
                  <th className="th">Artist</th>
                  {ARTIST_COLS.filter((c) => artistCols.includes(c.id)).map((c) => (
                    <SortHeader key={c.id} label={c.label} sort={artistSort} sortKey={c.sortKey} onSort={setArtistSort}
                      className={`relative ${ARTIST_COL_W[c.id] ?? "w-[14%]"}`}
                      style={artistW[c.id] ? { width: artistW[c.id] } : undefined}>
                      <ColumnResizer width={artistW[c.id]} onDrag={(w) => setArtistW(c.id, w)} onReset={() => resetArtistW()} />
                    </SortHeader>
                  ))}
                </tr>
              </thead>
              <tbody className="stagger">
                {sortedArtists.map((a) => {
                  const sel = selection.artists.includes(a.path);
                  return (
                    <tr key={a.path} className={`table-row group ${sel ? "bg-accent/15" : ""}`}>
                      {selectMode && (
                        <td className="td pr-0">
                          <input type="checkbox" className="" checked={sel} onChange={() => toggleArtist(a.path)} />
                        </td>
                      )}
                      <td className="td">
                        <Link to={artistRef(a)} className="font-medium hover:text-accent-soft">
                          {a.name}
                        </Link>
                      </td>
                      {artistCols.includes("albums") && <td className="td text-zinc-500">{a.aggregate.album_count}</td>}
                      {artistCols.includes("tracks") && <td className="td text-zinc-500">{a.aggregate.track_count}</td>}
                      {artistCols.includes("checks") && (
                        <td className="td text-zinc-500">{a.aggregate.pass_count}/{a.aggregate.total_checks}</td>
                      )}
                      {artistCols.includes("grade") && (
                        <td className="td"><GradeBadge pass={(a.aggregate.grade_pct ?? 0) >= 100 && !auditFails(a.aggregate.audit_summary)} score={a.aggregate.grade_pct} audit={a.aggregate.audit_summary} /></td>
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
            <table className="w-full text-sm">
              <thead className="border-b border-border">
                <tr>
                  {selectMode && (
                    <th className="th w-8">
                      <input type="checkbox" className="" checked={allTracksSelected}
                        onChange={() => setSelection({ tracks: allTracksSelected ? [] : sortedTracks.map((t) => t.path) })} />
                    </th>
                  )}
                  {TRACK_COLS.filter((c) => trackCols.includes(c.id)).map((c) => (
                    c.id === "cover" ? (
                      <th key={c.id} className={`th relative ${TRACK_COL_W[c.id] ?? ""}`} title="Cover art">
                        <span className="sr-only">Cover</span>
                      </th>
                    ) : (
                    <SortHeader key={c.id} label={c.label} sort={trackSort} sortKey={c.sortKey} onSort={setTrackSort}
                      className={`relative ${TRACK_COL_W[c.id] ?? ""}`}
                      style={trackW[c.id] ? { width: trackW[c.id] } : undefined}>
                      <ColumnResizer width={trackW[c.id]} onDrag={(w) => setTrackW(c.id, w)} onReset={() => resetTrackW()} />
                    </SortHeader>
                    )
                  ))}
                </tr>
              </thead>
              <tbody className="stagger">
                {sortedTracks.map((tr) => {
                  const sel = selection.tracks.includes(tr.path);
                  return (
                    <tr
                      key={tr.path}
                      className={`table-row group cursor-pointer ${sel ? "bg-accent/15" : ""}`}
                      title={selectMode ? "Click to select" : "Click to play"}
                      onClick={selectMode ? () => toggleTrack(tr.path) : () =>
                        playNow(
                          sortedTracks.map((t) => ({ path: t.path, file: t.file, albumPath: t.path.split("/").slice(0, -1).join("/"), artist: t.artist, album: t.album, title: t.tags.TITLE || undefined, coverFile: t.cover_file ?? null, albumCover: t.albumCover ?? null })),
                          sortedTracks.findIndex((t) => t.path === tr.path)
                        )
                      }
                    >
                      {selectMode && (
                        <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                          <input type="checkbox" className="" checked={sel} onChange={() => toggleTrack(tr.path)} />
                        </td>
                      )}
                      {trackCols.includes("num") && <td className="td cell-nowrap text-zinc-600">{tr.tracknumber ?? tr.tags.TRACKNUMBER ?? "—"}</td>}
                      {trackCols.includes("cover") && (
                        <td className="td cell-cover pr-0">
                          <TrackCover
                            albumPath={tr.albumPath ?? tr.path.split("/").slice(0, -1).join("/")}
                            trackCover={tr.cover_file}
                            albumCover={tr.albumCover}
                            wrapperClass="h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0"
                          />
                        </td>
                      )}
                      {trackCols.includes("title") && (
                        <td className="td">
                          <div className="flex items-center gap-1.5 min-w-0">
                            <Link
                              to={trackRef(tr)}
                              className="hover:text-accent-soft break-words flex-1 min-w-0"
                              title="Click to play · Ctrl-click to open track page"
                              onClick={(e) => entityLinkClick(e, () => navigate(trackRef(tr)))}
                            >
                              {tr.tags.TITLE ?? tr.file}
                            </Link>
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
                            <GradeBadge pass={!!tr.grade_pass && !auditFails(tr.audit)} audit={tr.audit} size="sm" />
                            <AdvisoryMark value={tr.tags.ITUNESADVISORY} />
                            {tr.is_video && <span title="Music video" className="shrink-0 inline-flex"><FileVideo className="h-3.5 w-3.5 text-zinc-500" /></span>}
                            <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                              <FavHeart kind="track" id={tr.path} mbid={tr.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" />
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
                            {tr.tags.INSTRUMENTAL === "1" && (
                              <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[10px] shrink-0">INST</span>
                            )}
                          </div>
                        </td>
                      )}
                      {trackCols.includes("artist") && <td className="td text-zinc-400 break-words">{tr.artist}</td>}
                      {trackCols.includes("album") && <td className="td text-zinc-500 break-words">{tr.album}</td>}
                      {trackCols.includes("year") && <td className="td text-zinc-500" title={tr.tags.DATE ?? undefined}>{fmtDateCell(tr.tags.DATE, fullDates)}</td>}
                      {trackCols.includes("genre") && <td className="td text-zinc-500 break-words">{tr.tags.GENRE ?? "—"}</td>}
                      {trackCols.includes("media") && <td className="td"><MediaChip media={tr.tags.MEDIA} /></td>}
                      {trackCols.includes("duration") && <td className="td text-zinc-500">{fmtDuration(tr.tech.length)}</td>}
                      {trackCols.includes("bitrate") && <td className="td text-zinc-500">{fmtTech(tr.tech) || "—"}</td>}
                      {trackCols.includes("dr") && (
                        <td className="td text-zinc-500 tabular-nums" title={`Dynamic range${tr.tags["ALBUM DYNAMIC RANGE"] ? ` · album ${tr.tags["ALBUM DYNAMIC RANGE"]}` : ""}`}>
                          {tr.tags["DYNAMIC RANGE"] ?? "—"}
                        </td>
                      )}
                      {trackCols.includes("source") && <td className="td text-zinc-500 break-words">{tr.tags.SOURCE ?? "—"}</td>}
                      {trackCols.includes("type") && (
                        <td className="td text-zinc-500" title={tr.is_video ? "Music video" : "Audio track"}>
                          {tr.is_video ? (
                            <span className="inline-flex items-center gap-1"><FileVideo className="h-3.5 w-3.5" /> Video</span>
                          ) : "Audio"}
                        </td>
                      )}
                      {trackCols.includes("inst") && (
                        <td className="td">
                          {tr.tags.INSTRUMENTAL === "1"
                            ? <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[10px]">INST</span>
                            : <span className="text-zinc-600">—</span>}
                        </td>
                      )}
                      {trackCols.includes("composer") && <td className="td text-zinc-500 break-words" title="Composer">{tr.tags.COMPOSER ?? "—"}</td>}
                      {trackCols.includes("lyricist") && <td className="td text-zinc-500 break-words" title="Lyricist">{tr.tags.LYRICIST ?? "—"}</td>}
                      {trackCols.includes("remixer") && <td className="td text-zinc-500 break-words" title="Remixer">{tr.tags.REMIXER ?? "—"}</td>}
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

function AlbumRowGroup({
  album,
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
  trackWidths: Record<string, number>;
  onTrackWidth: (id: string, px: number) => void;
  onResetTrackWidths: () => void;
}) {
  const navigate = useNavigate();
  const tracks = useMemo(() => [...(album.tracks ?? [])].sort(byDiscThenTrack), [album.tracks]);
  // The album-name cell IS the row title (AlbumRow renders it, with the link
  // and select-mode handling); the rest of the visible columns become cells.
  const showAlbumCol = visibleCols.includes("album");
  const cells: AlbumRowCell[] = [];
  if (visibleCols.includes("artist"))
    cells.push({ id: "artist", cls: "td text-zinc-400 break-words", node: album.artist });
  if (visibleCols.includes("year"))
    cells.push({
      id: "year", cls: "td text-zinc-500",
      title: album.meta?.ORIGINALDATE ?? album.meta?.DATE ?? undefined,
      node: fmtDateCell(album.meta?.ORIGINALDATE || album.meta?.DATE, fullDates),
    });
  if (visibleCols.includes("tracks"))
    cells.push({ id: "tracks", cls: "td text-zinc-500", node: album.track_count });
  if (visibleCols.includes("grade"))
    cells.push({
      id: "grade",
      node: <GradeBadge pass={!!album.pass && !auditFails(album.audit_summary)} score={album.grade_pct} audit={album.audit_summary} />,
    });
  if (visibleCols.includes("media")) cells.push({ id: "media", node: <MediaChip media={album.media} /> });
  if (visibleCols.includes("dr"))
    cells.push({
      id: "dr", cls: "td text-zinc-500 tabular-nums", title: "Album dynamic range",
      node: album.meta?.["ALBUM DYNAMIC RANGE"] ?? "—",
    });
  if (visibleCols.includes("source"))
    cells.push({ id: "source", cls: "td text-zinc-500 break-words", node: album.source_summary ?? "—" });
  if (visibleCols.includes("videos"))
    cells.push({
      id: "videos", cls: "td text-zinc-500 tabular-nums", title: "Music videos in this album",
      node: album.video_count || "—",
    });
  if (visibleCols.includes("inst"))
    cells.push({
      id: "inst", cls: "td text-zinc-500 tabular-nums", title: "Instrumental tracks in this album",
      node: album.inst_count || "—",
    });

  return (
    <>
      <AlbumRow
        title={showAlbumCol ? (album.meta?.ALBUM ?? album.path.split("/").pop()) : null}
        titleHref={albumRef(album)}
        titleExtra={
          showAlbumCol ? <AdvisoryMark value={album.meta?.ITUNESADVISORY ?? album.meta?.ALBUMITUNESADVISORY} /> : null
        }
        coverPath={album.path}
        coverFile={album.cover_file}
        coverTitle={selectMode ? "Click to select" : "Open album page"}
        cells={cells}
        actions={
          <>
            <button className="btn-ghost !px-1.5 !py-1" title="Add to playlist" onClick={onPlaylist}>
              <ListPlus className="h-3.5 w-3.5" />
            </button>
            <button className="btn-danger !px-1.5 !py-1" title="Remove album (to trash)" disabled={removing} onClick={onRemove}>
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
            <table className="w-full">
              <thead className="border-b border-border">
                <tr>
                  {selectMode && <th className="th w-8"></th>}
                  {ALBUM_TRACK_COLS.filter((c) => trackCols.includes(c.id)).map((c) =>
                    c.id === "cover" ? (
                      <th key={c.id} className={`th relative ${ALBUM_TRACK_COL_W[c.id] ?? ""}`} title="Cover art">
                        <span className="sr-only">Cover</span>
                      </th>
                    ) : (
                      <th key={c.id} className={`th relative ${ALBUM_TRACK_COL_W[c.id] ?? ""}`} style={trackWidths[c.id] ? { width: trackWidths[c.id] } : undefined}>
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
                            <td className="td text-zinc-600 tabular-nums cell-nowrap">
                              {groups.length > 1 ? `${g.disc}-${t.tracknumber ?? t.tags.TRACKNUMBER ?? "?"}` : t.tracknumber ?? t.tags.TRACKNUMBER ?? "—"}
                            </td>
                          )}
                          {trackCols.includes("cover") && (
                            <td className="td cell-cover pr-0">
                              <TrackCover
                                albumPath={album.path}
                                trackCover={t.cover_file}
                                wrapperClass="h-8 w-8 rounded bg-raise border border-border overflow-hidden shrink-0"
                              />
                            </td>
                          )}
                          {trackCols.includes("title") && (
                            <td className="td">
                              <div className="flex items-center gap-1.5 min-w-0">
                                <Link
                                  to={trackRef(t)}
                                  className="hover:text-accent-soft break-words flex-1 min-w-0"
                                  title="Click to play · Ctrl-click to open track page"
                                  onClick={(e) => entityLinkClick(e, () => navigate(trackRef(t)))}
                                >
                                  {t.tags.TITLE ?? t.file}
                                </Link>
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
                                <GradeBadge pass={!!t.grade_pass && !auditFails(t.audit)} audit={t.audit} size="sm" />
                                <AdvisoryMark value={t.tags.ITUNESADVISORY} />
                                {t.is_video && <span title="Music video" className="shrink-0 inline-flex"><FileVideo className="h-3.5 w-3.5 text-zinc-500" /></span>}
                                <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                                  <FavHeart kind="track" id={t.path} mbid={t.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" />
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
                                {t.tags.INSTRUMENTAL === "1" && (
                                  <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[10px] shrink-0">INST</span>
                                )}
                              </div>
                            </td>
                          )}
                          {trackCols.includes("genre") && <td className="td text-zinc-500 break-words">{t.tags.GENRE ?? "—"}</td>}
                          {trackCols.includes("dur") && <td className="td text-zinc-500">{fmtDuration(t.tech.length)}</td>}
                          {trackCols.includes("bitrate") && (
                            <td className="td text-zinc-500">{fmtTech(t.tech) || "—"}</td>
                          )}
                          {trackCols.includes("dr") && (
                            <td className="td text-zinc-500 tabular-nums" title={`Dynamic range${t.tags["ALBUM DYNAMIC RANGE"] ? ` · album ${t.tags["ALBUM DYNAMIC RANGE"]}` : ""}`}>
                              {t.tags["DYNAMIC RANGE"] ?? "—"}
                            </td>
                          )}
                        </tr>
                      ))}
                    </Fragment>
                  ));
                })()}
              </tbody>
            </table>
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
      <button className="btn-ghost !py-1 text-xs" onClick={() => setOpen(!open)}>
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

/** Sort state per table view, persisted like the column prefs (each view —
 * albums / artists / tracks — keeps its own key, so switching tabs or
 * reloading no longer resets the other tables). */
function useLocalSort(key: string): [SortState | null, (key: string) => void] {
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
