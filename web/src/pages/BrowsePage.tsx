/** Browse: the library asked a complex question.
 *
 *  Three parts, one engine. The builder writes a filter spec, the facet rail
 *  clicks values into that same spec, and the sheet below is the engine's own
 *  answer to it — the count in the builder, the total in the toolbar and the
 *  rows on screen all come from `/api/library/query`, so the page can never
 *  disagree with itself. "Save as smart playlist" hands the SAME spec to
 *  `POST /api/playlists` (kind "smart"): a saved playlist is that query, and
 *  evaluation later runs the very same code.
 *
 *  The rows are drawn in the library page's own row language — the shared badge
 *  components, formatters and table classes, and the same sortable headers —
 *  because the engine returns the library's row shape on purpose. */

import { Fragment, useMemo, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowDown, ArrowUp, ChevronDown, ChevronRight, Layers, Save, SlidersHorizontal,
} from "lucide-react";
import { api } from "../api";
import type { LibraryCondition } from "../api";
import { toast, useStore } from "../store";
import type { QueueTrack } from "../store";
import type { Album, Library, Track } from "../types";
import PageHeader from "../components/PageHeader";
import QueryBuilder, {
  NO_VALUE_OPS, fieldIndex, filterSpec, runnableConditions, useDebounced, useLibraryFields,
} from "../components/QueryBuilder";
import FacetRail from "../components/FacetRail";
import Segmented from "../components/Segmented";
import Modal from "../components/Modal";
import { AdvisoryMark, CachedMark, EmptyState, GradeBadge, MediaChip, PageLoading, PendingMark, pendingSummary } from "../components/Badges";
import { TrackCover } from "../components/CoverImg";
import FavHeart from "../components/FavHeart";
import { TrackActionsMenu } from "../components/TagActionsMenu";
import { ExportButton } from "../components/ExportDialog";
import { SortHeader, toggleSort } from "../lib/sort.tsx";
import type { SortState } from "../lib/sort.tsx";
import { TABLE_FIT } from "../lib/columns";
import { fmtDateCell, fmtDuration, fmtTech } from "../lib/fmt";
import { useI18n } from "../lib/i18n";
import { albumRef, entityLinkClick, trackRef } from "../lib/refs";

type Target = "tracks" | "albums";

/** Rows per page. The result is paged rather than virtualised: the engine
 *  pages server-side (offset/limit), and a page of this size keeps the table
 *  cheap to draw while the pager shows exactly where in the result the user is. */
const PAGE = 200;

/** Grouping keys the engine accepts. */
const GROUPS: { id: string; label: string }[] = [
  { id: "artist", label: "Artist" },
  { id: "album", label: "Album" },
  { id: "genre", label: "Genre" },
  { id: "year", label: "Year" },
  { id: "rating", label: "Rating" },
];

interface ResultCol {
  id: string;
  label: string;
  /** The engine's row key for this column ("" = the column is not sortable). */
  sortKey: string;
  /** Column floor, same rule as the library page's tables. */
  width: string;
  hideOnPhone?: boolean;
}

const TRACK_RESULT_COLS: ResultCol[] = [
  { id: "num", label: "#", sortKey: "library.tracknumber", width: "w-12", hideOnPhone: true },
  { id: "cover", label: "", sortKey: "", width: "w-[52px]" },
  { id: "title", label: "Title", sortKey: "tags.TITLE", width: "md:w-[220px]" },
  { id: "artist", label: "Artist", sortKey: "artist.name", width: "w-[120px]", hideOnPhone: true },
  { id: "album", label: "Album", sortKey: "album.name", width: "w-[140px]", hideOnPhone: true },
  { id: "year", label: "Year", sortKey: "tags.DATE", width: "w-16", hideOnPhone: true },
  { id: "genre", label: "Genre", sortKey: "tags.GENRE", width: "w-24", hideOnPhone: true },
  { id: "duration", label: "Duration", sortKey: "tech.length", width: "w-[84px]", hideOnPhone: true },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate", width: "w-[124px]", hideOnPhone: true },
  { id: "rating", label: "Rating", sortKey: "rating", width: "w-[68px]" },
  { id: "grade", label: "Grade", sortKey: "grade_pass", width: "w-20" },
];

const ALBUM_RESULT_COLS: ResultCol[] = [
  { id: "album", label: "Album", sortKey: "album.name", width: "md:w-[240px]" },
  { id: "artist", label: "Artist", sortKey: "artist.name", width: "w-[140px]" },
  { id: "year", label: "Year", sortKey: "album.year", width: "w-16", hideOnPhone: true },
  { id: "tracks", label: "Tracks", sortKey: "album.track_count", width: "w-16", hideOnPhone: true },
  { id: "grade", label: "Grade", sortKey: "grade_pct", width: "w-20" },
  { id: "media", label: "Media", sortKey: "album.media", width: "w-[88px]", hideOnPhone: true },
];

const PHONE_HIDE = " hidden md:table-cell";

/** One group of rows as the sheet renders it: the engine's key, the count it
 *  reported for the WHOLE matched set, and the rows that fell on this page. */
interface GroupSlice {
  key: string;
  count: number;
  rows: Record<string, any>[];
}

/** Cut one page's rows into groups. Items arrive sorted by group key, so a
 *  run-length walk is enough; a key the engine reported no count for (a page
 *  boundary, an empty value) falls back to what is on screen. */
function sliceGroups(items: Record<string, any>[], counts: Map<string, number>): GroupSlice[] {
  const out: GroupSlice[] = [];
  let cur: GroupSlice | null = null;
  for (const row of items) {
    const key = String(row.group ?? "");
    if (!cur || cur.key !== key) {
      cur = { key, count: counts.get(key) ?? 0, rows: [] };
      out.push(cur);
    }
    cur.rows.push(row);
  }
  for (const g of out) if (!g.count) g.count = g.rows.length;
  return out;
}

/** One track row's per-album facts, from the library payload. The engine
 *  stamps `artist`/`album` on a track row already; this map is what fills in
 *  the album's cover and the advisory mark, and what keeps play/queue entries
 *  complete for both targets. */
interface TrackMeta {
  artist: string;
  album: string;
  albumPath: string;
  albumCover: string | null;
  title: string;
  coverFile: string | null;
  advisory: string | null;
  dur: number;
}

function trackMetaOf(lib: Library | undefined): Map<string, TrackMeta> {
  const map = new Map<string, TrackMeta>();
  const key = (p: string) => p.replace(/\\/g, "/");
  for (const a of lib?.artists ?? []) {
    for (const al of a.albums) {
      for (const t of al.tracks) {
        map.set(key(t.path), {
          artist: al.album_artist || a.name,
          album: al.meta?.ALBUM || al.path.split("/").pop() || "",
          albumPath: al.path,
          albumCover: al.cover_file,
          title: t.tags.TITLE ?? t.file,
          coverFile: t.cover_file ?? null,
          advisory: t.tags.ITUNESADVISORY ?? null,
          dur: t.tech?.length ?? 0,
        });
      }
    }
  }
  return map;
}

function queueEntry(meta: TrackMeta | undefined, path: string): QueueTrack {
  const file = path.split("/").pop() ?? path;
  return {
    path,
    file,
    albumPath: meta?.albumPath ?? path.split("/").slice(0, -1).join("/"),
    artist: meta?.artist,
    album: meta?.album,
    title: meta?.title,
    coverFile: meta?.coverFile ?? null,
    albumCover: meta?.albumCover ?? null,
    advisory: meta?.advisory ?? null,
  };
}

/** Rating in the UI's own 0-5 scale: the engine stamps half-star steps on the
 *  row (falling back to the RATING tag). 0 is "unrated", never "no stars". */
function ratingText(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) && n > 0 ? (Math.round(n * 2) / 2).toFixed(1).replace(/\.0$/, "") : "—";
}

export default function BrowsePage() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { playNow } = useStore();
  const { data: catalogue } = useLibraryFields();
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: () => api.library() });

  const [conditions, setConditions] = useState<LibraryCondition[]>([]);
  const [match, setMatch] = useState<"all" | "any">("all");
  const [target, setTarget] = useState<Target>("tracks");
  const [sort, setSort] = useState<SortState>({ key: "artist.name", dir: 1 });
  const [group, setGroup] = useState("");
  const [page, setPage] = useState(0);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [saveOpen, setSaveOpen] = useState(false);
  const [name, setName] = useState("");

  const fields = useMemo(() => fieldIndex(catalogue), [catalogue]);
  /** Every key the engine will sort by: the catalogue's own sortable fields.
   *  A header is clickable only when its key is in here (or is a grouping key),
   *  so the UI can never ask for a sort the engine would refuse. */
  const sortable = useMemo(() => {
    const keys = new Set<string>(GROUPS.map((g) => g.id));
    for (const f of fields.values()) if (f.sortable) keys.add(f.field);
    return keys;
  }, [fields]);
  /** The catalogue's own sortable fields for the CURRENT target, grouped as
   *  the catalogue groups them — a tracks-only key has nothing to sort album
   *  rows by and would read as a sort that does nothing. */
  const sortGroups = useMemo(
    () =>
      (catalogue?.groups ?? [])
        .map((g) => ({
          label: g.label,
          fields: g.fields.filter((f) => f.sortable && (!f.targets?.length || f.targets.includes(target))),
        }))
        .filter((g) => g.fields.length),
    [catalogue, target]
  );
  const meta = useMemo(() => trackMetaOf(lib), [lib]);

  const spec = useMemo(() => filterSpec(conditions, match), [conditions, match]);
  const request = useMemo(
    () => ({
      ...spec,
      target,
      sort: { key: sortable.has(sort.key) ? sort.key : "library.path", dir: sort.dir },
      group: group || null,
      limit: PAGE,
      offset: page * PAGE,
    }),
    [spec, target, sort, group, page, sortable]
  );
  // Typing in a value box must not fire a query per keystroke; the same
  // debounce the count uses, on the whole request.
  const settledKey = useDebounced(JSON.stringify(request));
  const settled = useMemo(() => JSON.parse(settledKey) as typeof request, [settledKey]);
  const { data, isFetching, error, refetch } = useQuery({
    queryKey: ["libraryQuery", "page", settledKey],
    // The target rides WITH the rows rather than beside them: while a switch
    // between tracks and albums is in flight the previous page is still on
    // screen (placeholderData), and drawing track rows with the album renderer
    // — or the reverse — reads fields that are not there and takes the page
    // down. So every row decision below follows the data's own target and
    // catches up with the toolbar a moment later.
    queryFn: async () => ({ ...(await api.libraryQuery(settled)), target: settled.target }),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });

  const rowsTarget = data?.target ?? target;
  const cols = rowsTarget === "tracks" ? TRACK_RESULT_COLS : ALBUM_RESULT_COLS;
  // Memoised: `?? []` would hand every memo below a fresh array each render.
  const items = useMemo(() => data?.items ?? [], [data]);
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const counts = useMemo(
    () => new Map((data?.group_counts ?? []).map((g) => [String(g.key), g.count])),
    [data]
  );
  const slices = useMemo(() => (group ? sliceGroups(items, counts) : null), [group, items, counts]);

  // A new question starts at its own first page: paging to page 7 and then
  // changing a condition would otherwise land on an empty sheet. This is a
  // render-time reset rather than an effect — the request below then already
  // asks for page 0, so the sheet never flashes a page that no longer exists.
  const specKey = settledKey.replace(/"offset":\d+/, "");
  const [pageKey, setPageKey] = useState(specKey);
  if (pageKey !== specKey) {
    setPageKey(specKey);
    setPage(0);
  }

  const loadedTracks = useMemo(() => {
    if (rowsTarget === "tracks") return items as Track[];
    return items.flatMap((a) => ((a as Album).tracks ?? []) as Track[]);
  }, [items, rowsTarget]);
  const exportPaths = useMemo(() => loadedTracks.map((t) => t.path), [loadedTracks]);
  const exportSeconds = useMemo(
    () => loadedTracks.reduce((s, t) => s + (t.tech?.length ?? meta.get(t.path.replace(/\\/g, "/"))?.dur ?? 0), 0),
    [loadedTracks, meta]
  );
  const truncated = items.length < total;

  const play = (from: string) => {
    if (!loadedTracks.length) return;
    const queue = loadedTracks.map((t) => queueEntry(meta.get(t.path.replace(/\\/g, "/")), t.path));
    const i = Math.max(0, loadedTracks.findIndex((t) => t.path === from));
    playNow(queue, i);
  };

  /** A facet's selection becomes ONE condition (eq + list = "is any of"),
   *  replacing EVERY value condition that field had: the values of one field
   *  are an OR group, and a group spread over several rows would be AND-ed
   *  with itself by `match: all` — "genre is shoegaze AND genre is one of
   *  shoegaze, art rock" — which matches nothing. The rail is the control for
   *  its field's values, so what it shows checked is exactly what the query
   *  holds for that field, whatever shape the row arrived in.
   *  The "no value" row is a condition of its own (empty/rating ops take no
   *  value), so it is added beside the group rather than inside it. */
  const applyFacet = (field: string, values: (string | number)[] | null, noValue?: string | null) => {
    setConditions((cs) => {
      const rest = cs.filter((c) => !(c.field === field && (c.op === "eq" || NO_VALUE_OPS[c.op])));
      const next = [...rest];
      if (values?.length) next.push({ field, op: "eq", value: values });
      if (noValue) next.push({ field, op: noValue });
      return next;
    });
  };

  const save = useMutation({
    mutationFn: (playlistName: string) => api.createPlaylist(playlistName, "smart", spec),
    onSuccess: (pl) => {
      qc.invalidateQueries({ queryKey: ["playlists"] });
      setSaveOpen(false);
      setName("");
      toast(`Smart playlist "${pl.name}" saved — ${total.toLocaleString()} matching ${target}`);
      // Straight to the playlist: its own page evaluates the saved filter with
      // the same engine, which is where a user checks it saved what they built.
      navigate(`/playlist/${pl.id}`);
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  const pending = conditions.length - runnableConditions(conditions).length;
  const errorText = error ? (error instanceof Error ? error.message : String(error)) : null;

  const sortControl = (
    <span className="flex items-center gap-1">
      <select
        className="input !py-1 text-xs w-[190px]"
        value={sort.key}
        onChange={(e) => setSort({ key: e.target.value, dir: sort.dir })}
        title="Sort by a field the engine can sort on"
        aria-label="Sort field"
      >
        <optgroup label="Grouping">
          {GROUPS.map((g) => (
            <option key={g.id} value={g.id}>
              {g.label}
            </option>
          ))}
        </optgroup>
        {sortGroups.map((g) => (
          <optgroup key={g.label} label={g.label}>
            {g.fields.map((f) => (
              <option key={f.field} value={f.field}>
                {f.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      <button
        className="btn-ghost !px-2"
        title={sort.dir === 1 ? "Ascending — click for descending" : "Descending — click for ascending"}
        aria-label="Sort direction"
        onClick={() => setSort({ key: sort.key, dir: sort.dir === 1 ? -1 : 1 })}
      >
        {sort.dir === 1 ? <ArrowUp className="h-3.5 w-3.5" /> : <ArrowDown className="h-3.5 w-3.5" />}
      </button>
    </span>
  );

  const groupControl = (
    <select
      className="input !py-1 text-xs w-[150px]"
      value={group}
      onChange={(e) => setGroup(e.target.value)}
      title="Collect the result into groups"
      aria-label="Group by"
    >
      <option value="">No grouping</option>
      {GROUPS.map((g) => (
        <option key={g.id} value={g.id}>
          Group by {g.label}
        </option>
      ))}
    </select>
  );

  const renderTrackRow = (tr: Track) => {
    const m = meta.get(tr.path.replace(/\\/g, "/"));
    const artist = (tr as Track & { artist?: string }).artist ?? m?.artist ?? "";
    const albumName = (tr as Track & { album?: string }).album ?? m?.album ?? "";
    return (
      <tr key={tr.path} className="table-row group cursor-pointer" title="Click to play" onClick={() => play(tr.path)}>
        <td className={`td cell-nowrap text-zinc-600${PHONE_HIDE}`}>{tr.tracknumber ?? tr.tags.TRACKNUMBER ?? "—"}</td>
        <td className="td cell-cover pr-0">
          <TrackCover
            albumPath={m?.albumPath ?? tr.path.split("/").slice(0, -1).join("/")}
            trackCover={tr.cover_file}
            albumCover={m?.albumCover}
            wrapperClass="h-9 w-9 rounded bg-raise overflow-hidden shrink-0"
          />
        </td>
        <td className="td">
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 min-w-0">
            <Link
              to={trackRef(tr)}
              className="hover:text-accent-soft break-words flex-1 min-w-[8rem]"
              title="Click to play · Ctrl-click to open track page"
              onClick={(e) => entityLinkClick(e, () => navigate(trackRef(tr)))}
            >
              {tr.tags.TITLE ?? tr.file}
            </Link>
            <AdvisoryMark value={tr.tags.ITUNESADVISORY} />
            <CachedMark path={tr.path} />
            <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
              <FavHeart kind="track" id={tr.path} mbid={tr.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" revealOnHover />
            </span>
            <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
              <TrackActionsMenu path={tr.path} releaseMbid={tr.tags.MUSICBRAINZ_ALBUMID} />
            </span>
          </div>
        </td>
        <td className={`td text-zinc-400 break-words${PHONE_HIDE}`}>{artist || "—"}</td>
        <td className={`td text-zinc-500 break-words${PHONE_HIDE}`}>{albumName || "—"}</td>
        <td className={`td text-zinc-500${PHONE_HIDE}`} title={tr.tags.DATE ?? undefined}>
          {fmtDateCell(tr.tags.DATE, false)}
        </td>
        <td className={`td text-zinc-500 break-words${PHONE_HIDE}`}>{tr.tags.GENRE ?? "—"}</td>
        <td className={`td text-zinc-500${PHONE_HIDE}`}>{fmtDuration(tr.tech?.length)}</td>
        <td className={`td text-zinc-500${PHONE_HIDE}`}>{fmtTech(tr.tech) || "—"}</td>
        <td className="td text-zinc-400 tabular-nums" title="Rating (0-5, half stars)">
          {ratingText((tr as Track & { rating?: number }).rating ?? (tr.tags as Record<string, unknown>).RATING)}
        </td>
        <td className="td">
          <GradeBadge pass={!!tr.grade_pass} score={null} size="sm" />
        </td>
      </tr>
    );
  };

  const renderAlbumRow = (al: Album) => {
    const artist = al.album_artist ?? "";
    const albumName = al.meta?.ALBUM ?? al.path.split("/").pop() ?? al.path;
    // A framework album has no audio: the row is not a play button, and it
    // says why instead of starting whatever else the page holds.
    const pending = pendingSummary(al, t);
    return (
      <tr
        key={al.path}
        className={`table-row group${pending ? "" : " cursor-pointer"}`}
        title={pending ? pending.full : "Click to play the album"}
        onClick={pending ? undefined : () => play(al.tracks?.[0]?.path ?? "")}
      >
        <td className="td">
          <div className="flex items-center gap-1.5 min-w-0">
            <Link
              to={albumRef(al)}
              className="hover:text-accent-soft break-words font-medium"
              onClick={(e) => entityLinkClick(e, () => navigate(albumRef(al)))}
            >
              {albumName}
            </Link>
            <FavHeart kind="album" id={al.path} iconClass="h-3.5 w-3.5" />
            {/* the same marker every other album-shaped surface carries */}
            <PendingMark album={al} />
          </div>
        </td>
        <td className="td text-zinc-400 break-words">{artist || "—"}</td>
        <td className={`td text-zinc-500${PHONE_HIDE}`}>{fmtDateCell(al.meta?.DATE, false)}</td>
        <td className={`td text-zinc-500 tabular-nums${PHONE_HIDE}`}>{al.track_count}</td>
        <td className="td">
          <GradeBadge pass={al.pass} score={al.grade_pct} size="sm" />
        </td>
        <td className={`td${PHONE_HIDE}`}>
          <MediaChip media={al.media} />
        </td>
      </tr>
    );
  };

  const head = () => (
    <thead className="border-b border-border">
      <tr>
        {cols.map((c) =>
          c.sortKey && sortable.has(c.sortKey) ? (
            <SortHeader
              key={c.id}
              label={c.label}
              sort={sort}
              sortKey={c.sortKey}
              onSort={(k) => setSort((cur) => toggleSort(cur, k))}
              className={`relative ${c.width}${c.hideOnPhone ? PHONE_HIDE : ""}`}
            />
          ) : (
            <th key={c.id} className={`th ${c.width}${c.hideOnPhone ? PHONE_HIDE : ""}`}>
              {c.id === "cover" ? <span className="sr-only">Cover</span> : c.label}
            </th>
          )
        )}
      </tr>
    </thead>
  );

  return (
    <div className="p-6 space-y-4 mx-auto max-w-[1600px]">
      <PageHeader
        icon={SlidersHorizontal}
        overline="Library"
        title="Browse"
        subtitle="Build a query over tags, ratings, grades, technical facts and audio analysis — then keep it as a smart playlist."
        actions={
          <>
            <button className="btn" disabled={pending > 0 && !conditions.length} onClick={() => setSaveOpen(true)} title="Store this query as a smart playlist">
              <Save className="h-4 w-4" /> Save as smart playlist
            </button>
            <ExportButton
              paths={exportPaths}
              seconds={exportSeconds}
              label="Export"
              size="md"
              title="Export the rows on screen"
              emptyReason="Nothing to export yet"
              dialogSubtitle={
                truncated
                  ? `${exportPaths.length} of ${total.toLocaleString()} matching tracks — the rest are on later pages`
                  : `${exportPaths.length} track${exportPaths.length === 1 ? "" : "s"} from this query`
              }
            />
          </>
        }
      >
        <QueryBuilder
          conditions={conditions}
          match={match}
          target={target}
          onChange={({ conditions: cs, match: m }) => {
            setConditions(cs);
            setMatch(m);
          }}
          className="rounded-xl border border-border bg-panel/60 p-3"
        />
      </PageHeader>

      <div className="grid gap-4 md:grid-cols-[260px_minmax(0,1fr)] items-start">
        <FacetRail applied={conditions} target={target} onApply={applyFacet} />

        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Segmented
              value={target}
              // The row shape changes with the target, so the sort starts over
              // on that target's own default: a track key left in place (a
              // bitrate, a duration) has nothing to sort album rows by, and a
              // select that names a column the sheet does not show reads as a
              // broken sort rather than as a no-op.
              onChange={(t) => {
                setTarget(t);
                setSort({ key: t === "tracks" ? "artist.name" : "album.name", dir: 1 });
              }}
              options={[
                { id: "tracks", label: "Tracks" },
                { id: "albums", label: "Albums" },
              ]}
            />
            {sortControl}
            {groupControl}
            <span className="text-xs text-zinc-500 ml-auto" title={data ? `the engine answered in ${data.took_ms} ms` : undefined}>
              {/* "Not asked yet" and "0 results" are different facts: before the
                  engine has answered there IS no count, and printing "0 tracks"
                  made a page that had not run look like a page that found
                  nothing. */}
              {data
                ? `${total.toLocaleString()} ${rowsTarget}${isFetching ? " · counting…" : ""}`
                : t("browse.not_asked")}
            </span>
          </div>

          {errorText ? (
            <EmptyState
              title={t("browse.error_title")}
              hint={errorText}
              onAction={{ label: t("action.retry"), onClick: () => void refetch(), disabled: isFetching }}
            />
          ) : !data && !items.length ? (
            <PageLoading label="Running the query…" />
          ) : !items.length ? (
            <EmptyState
              title={spec.conditions.length ? t("browse.zero_title") : t("browse.empty_title")}
              hint={
                spec.conditions.length
                  ? t("browse.zero_hint", {
                      n: runnableConditions(conditions).length,
                      target: rowsTarget === "tracks" ? t("charts.kind.tracks") : t("charts.kind.albums"),
                    })
                  : t("browse.empty_hint", {
                      target: rowsTarget === "tracks" ? t("charts.kind.tracks") : t("charts.kind.albums"),
                    })
              }
              onAction={
                spec.conditions.length
                  ? { label: t("browse.clear"), onClick: () => setConditions([]) }
                  : undefined
              }
            />
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className={`${TABLE_FIT} text-sm`}>
                  {head()}
                  <tbody className="stagger">
                    {slices
                      ? slices.map((g) => {
                          const shut = collapsed.has(g.key);
                          return (
                            <Fragment key={g.key}>
                              <tr className="bg-raise/60 cursor-pointer" onClick={() =>
                                setCollapsed((cur) => {
                                  const next = new Set(cur);
                                  if (next.has(g.key)) next.delete(g.key); else next.add(g.key);
                                  return next;
                                })
                              }>
                                <td className="td py-1.5 text-xs font-semibold text-zinc-300" colSpan={cols.length}>
                                  <span className="inline-flex items-center gap-1.5">
                                    {shut ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                                    <Layers className="h-3.5 w-3.5 text-zinc-600" />
                                    {g.key || "(empty)"}
                                    <span className="text-zinc-500 font-normal">
                                      {g.count.toLocaleString()} {rowsTarget}
                                      {g.rows.length < g.count ? ` · ${g.rows.length} on this page` : ""}
                                    </span>
                                  </span>
                                </td>
                              </tr>
                              {!shut &&
                                g.rows.map((row) => (rowsTarget === "tracks" ? renderTrackRow(row as Track) : renderAlbumRow(row as Album)))}
                            </Fragment>
                          );
                        })
                      : items.map((row) => (rowsTarget === "tracks" ? renderTrackRow(row as Track) : renderAlbumRow(row as Album)))}
                  </tbody>
                </table>
              </div>

              <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-500">
                <span className="tabular-nums">
                  {page * PAGE + 1}–{page * PAGE + items.length} of {total.toLocaleString()} {rowsTarget}
                </span>
                <span className="flex items-center gap-1.5 ml-auto">
                  <button className="btn-ghost !py-1 text-xs" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
                    Previous
                  </button>
                  <span className="tabular-nums">
                    page {page + 1} / {pages}
                  </span>
                  <button
                    className="btn-ghost !py-1 text-xs"
                    disabled={page + 1 >= pages}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    Next
                  </button>
                </span>
              </div>
            </>
          )}
        </div>
      </div>

      {saveOpen && (
        <Modal
          onClose={() => setSaveOpen(false)}
          icon={Save}
          title="Save as smart playlist"
          subtitle="The playlist stores this filter and is evaluated by the same engine, so it matches exactly these rows."
          width="max-w-md"
          footer={
            <div className="flex justify-end gap-2">
              <button className="btn-ghost" onClick={() => setSaveOpen(false)}>
                Cancel
              </button>
              <button className="btn-primary" disabled={!name.trim() || save.isPending} onClick={() => save.mutate(name.trim())}>
                {save.isPending ? "Saving…" : "Save playlist"}
              </button>
            </div>
          }
        >
          <input
            className="input"
            placeholder="Playlist name"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && name.trim() && !save.isPending) save.mutate(name.trim());
            }}
          />
          <div className="mt-3 text-xs text-zinc-500 space-y-1">
            <div>
              {spec.conditions.length} condition{spec.conditions.length === 1 ? "" : "s"}, matching{" "}
              {match === "all" ? "all of them" : "any of them"} — {total.toLocaleString()} {rowsTarget} right now.
            </div>
            {pending > 0 && (
              <div className="text-amber-300/80">
                {pending} incomplete condition{pending === 1 ? " is" : "s are"} not part of the query (a row needs a value),
                so {pending === 1 ? "it" : "they"} will not be saved either.
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
