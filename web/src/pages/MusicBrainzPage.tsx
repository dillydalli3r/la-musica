import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  keepPreviousData, useInfiniteQuery, useQuery, useQueryClient,
} from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ArrowUpDown, ArrowUpRight, BookmarkPlus, Check, Loader2, Search, Zap } from "lucide-react";
import { api } from "../api";
import { EmptyState } from "../components/Badges";
import { MbIcon } from "../components/Links";
import Segmented from "../components/Segmented";
import { toast } from "../store";

/* In-app MusicBrainz browser: search across the four browsable entities and
 * drill into artist / release-group / release / recording pages. Everything
 * renders as column tables (same language as the library views), pages 100
 * rows at a time with load-more footers, keeps the previous results visible
 * while a new query loads, and prefetches entity pages on row hover. Bare
 * MusicBrainz IDs and musicbrainz.org links pasted into the search box are
 * detected and routed to their entity page. */

const PAGE = 100;

const TYPES = [
  { id: "artist", label: "Artists" },
  { id: "release-group", label: "Release groups" },
  { id: "release", label: "Releases" },
  { id: "recording", label: "Recordings" },
] as const;
type MBType = (typeof TYPES)[number]["id"];

/** MusicBrainz release types, split the way MusicBrainz splits them: a
 *  release group has ONE primary type (Album / Single / EP / Broadcast /
 *  Other) plus any number of secondary types (Soundtrack / Live /
 *  Compilation / Remix / DJ-mix / Mixtape/Street / Demo / Spokenword /
 *  Interview / Audiobook / Audio drama / Field recording). "Score album"
 *  lives as Album + Soundtrack, never as its own primary type. */
const PRIMARY_TYPES = ["Album", "Single", "EP", "Broadcast", "Other"] as const;
const SECONDARY_TYPES = [
  "Compilation", "Soundtrack", "Spokenword", "Interview", "Audiobook",
  "Audio drama", "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo",
  "Field recording",
] as const;

/** "Album + Soundtrack" — the full type of a release/release group. */
const typeLabel = (
  primary?: string | null,
  secondary?: readonly string[] | null
) => [primary, ...(secondary ?? [])].filter(Boolean).join(" + ");

/** app route path for an entity ("release-group" browses at /mb/rg/…) */
const routeFor = (type: string) =>
  type === "release-group" ? "rg" : type === "release" ? "release" : type;

const MB_URL_RE = /musicbrainz\.org\/(artist|release-group|release|recording)\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
const MBID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface RGRow {
  id: string; title: string; primary_type?: string; secondary_types?: string[];
  first_release_date?: string;
}
interface MBTrackRow {
  disc: number; position: number; title: string; length?: number | null;
  recording_mbid?: string | null; artist_credit?: string;
}
interface RelRow {
  id: string; title: string; date?: string; country?: string; status?: string;
  formats?: string; disc_count?: number; track_count?: number;
  track_breakdown?: string; barcode?: string; release_group?: string;
  disambiguation?: string;
  /** the release group's type: primary + secondary ("Album + Soundtrack") */
  primary_type?: string; secondary_types?: string[];
}
const mbUrl = (type: string, id: string) =>
  `https://musicbrainz.org/${type === "release-group" ? "release-group" : type}/${id}`;

/** '2 discs · 14 + 5' for multi-disc editions, plain count otherwise. */
const tracksLabel = (r: RelRow) => {
  if (!r.track_count && !r.track_breakdown) return "";
  if ((r.disc_count ?? 1) > 1) return `${r.track_breakdown}`;
  return `${r.track_count}`;
};

const fmtLen = (ms?: number | null) => {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

function ExtLink({ href, title }: { href: string; title: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title={title}
      className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors shrink-0"
      onClick={(e) => e.stopPropagation()}
    >
      <MbIcon className="h-4 w-4" />
    </a>
  );
}

function Spinner() {
  return (
    <div className="flex items-center justify-center gap-2 py-24 text-sm text-zinc-500">
      <Loader2 className="h-4 w-4 animate-spin" /> Asking MusicBrainz…
    </div>
  );
}

function LoadError({ e }: { e: unknown }) {
  return (
    <EmptyState
      title="MusicBrainz request failed"
      hint={String(e instanceof Error ? e.message : e)}
    />
  );
}

/** Footer under a paged list: what's shown, and a load-more control. */
function LoadMore({ loaded, total, busy, onLoad }: {
  loaded: number; total: number; busy: boolean; onLoad: () => void;
}) {
  if (loaded >= total) return null;
  return (
    <button
      className="w-full py-2 text-xs text-zinc-400 hover:text-white hover:bg-raise transition-colors disabled:opacity-50 flex items-center justify-center gap-1.5"
      onClick={onLoad}
      disabled={busy}
    >
      {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
      Load more — showing {loaded} of {total}
    </button>
  );
}

/* ---- client-side column sorting ------------------------------------------ */

interface SortState { key: string; dir: 1 | -1 }

function useSort<T extends Record<string, any>>(rows: T[], defaultKey: string | null) {
  // defaultKey=null keeps the source order (MusicBrainz ranks searches by
  // relevance) until the user clicks a column header.
  const [sort, setSort] = useState<SortState | null>(
    defaultKey ? { key: defaultKey, dir: 1 } : null
  );
  const onSort = (key: string) =>
    setSort((s) => (s && s.key === key ? { key, dir: (s.dir === 1 ? -1 : 1) } : { key, dir: 1 }));
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const val = (r: T) => {
      const v = r[sort.key];
      return v == null ? (typeof v === "number" ? 0 : "") : v;
    };
    return [...rows].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * sort.dir;
      return String(va).localeCompare(String(vb), undefined, { numeric: true }) * sort.dir;
    });
  }, [rows, sort]);
  return { sort, onSort, sorted };
}

function SortTh({ label, k, sort, onSort, className }: {
  label: string; k: string; sort: SortState | null; onSort: (k: string) => void; className?: string;
}) {
  return (
    <th className={`th cursor-pointer select-none hover:text-zinc-300 ${className ?? ""}`} onClick={() => onSort(k)}>
      <span className="inline-flex items-center gap-1">
        {label}
        {sort && sort.key === k ? (
          sort.dir === 1 ? <ArrowUp className="h-3 w-3 text-accent" /> : <ArrowDown className="h-3 w-3 text-accent" />
        ) : (
          <ArrowUpDown className="h-3 w-3 opacity-30" />
        )}
      </span>
    </th>
  );
}

/** Warm an entity page while the pointer is over a row that links to it —
 * by click time the payload is usually already in the query cache. */
function useMbPrefetch() {
  const qc = useQueryClient();
  return (type: string, id: string) => {
    if (!id) return;
    if (type === "artist") {
      qc.prefetchInfiniteQuery({
        queryKey: ["mbArtist", id],
        queryFn: ({ pageParam }: any) => api.mbArtist(id, pageParam ?? 0),
        initialPageParam: 0,
      });
    } else if (type === "release-group") {
      qc.prefetchInfiniteQuery({
        queryKey: ["mbRG", id],
        queryFn: ({ pageParam }: any) => api.mbReleaseGroup(id, pageParam ?? 0),
        initialPageParam: 0,
      });
    } else if (type === "release") {
      qc.prefetchQuery({ queryKey: ["mbRelease", id], queryFn: () => api.mbRelease(id) });
    } else if (type === "recording") {
      qc.prefetchInfiniteQuery({
        queryKey: ["mbRecording", id],
        queryFn: ({ pageParam }: any) => api.mbRecording(id, pageParam ?? 0),
        initialPageParam: 0,
      });
    }
  };
}

/** Page header shared by the detail views: title, meta line, chips + actions. */
function PageHeader({
  overline,
  title,
  meta,
  chips,
  mbHref,
  soulseekQuery,
  children,
}: {
  overline: string;
  title: string;
  meta?: string;
  chips?: string[];
  mbHref?: string;
  /** what the Soulseek handoff button searches (defaults to the title) */
  soulseekQuery?: string;
  children?: React.ReactNode;
}) {
  const nav = useNavigate();
  return (
    <div className="p-6 pb-4 max-w-4xl mx-auto">
      <Link to="/mb/search" className="text-[11px] text-zinc-500 hover:text-zinc-300">
        ← MusicBrainz search
      </Link>
      <div className="flex items-start justify-between gap-4 mt-2">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500">{overline}</div>
          <h1 className="text-2xl font-bold text-white truncate" title={title}>
            {title}
          </h1>
          {meta && <div className="text-sm text-zinc-400 mt-1">{meta}</div>}
          {chips && chips.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {chips.map((c) => (
                <span key={c} className="chip bg-raise border border-border text-zinc-300">
                  {c}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {children}
          {mbHref && <ExtLink href={mbHref} title="Open on MusicBrainz" />}
          <button
            className="btn-ghost !py-1.5 text-xs"
            title="Search Soulseek for this"
            onClick={() => nav(`/soulseek?q=${encodeURIComponent(soulseekQuery ?? title)}`)}
          >
            <Search className="h-3.5 w-3.5" /> Soulseek
          </button>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Search                                                              */
/* ------------------------------------------------------------------ */

/** Column layout per entity — same table language as the library views. */
const COLUMNS: Record<MBType, { k: string; label: string; className?: string }[]> = {
  artist: [
    { k: "title", label: "Name" },
    { k: "type", label: "Type", className: "w-[10%]" },
    { k: "country", label: "Country", className: "w-[12%]" },
    { k: "life", label: "Life span", className: "w-[16%]" },
    { k: "tags", label: "Tags", className: "w-[18%]" },
    { k: "score", label: "Score", className: "w-20 cell-nowrap text-right" },
  ],
  "release-group": [
    { k: "title", label: "Title" },
    { k: "artist", label: "Artist", className: "w-[24%]" },
    { k: "typeLabel", label: "Type", className: "w-[14%]" },
    { k: "first_release_date", label: "First released", className: "w-[12%]" },
    { k: "score", label: "Score", className: "w-20 cell-nowrap text-right" },
  ],
  release: [
    { k: "title", label: "Title" },
    { k: "artist", label: "Artist", className: "w-[20%]" },
    { k: "typeLabel", label: "Type", className: "w-[11%]" },
    { k: "date", label: "Date", className: "w-[9%]" },
    { k: "formats", label: "Format", className: "w-[11%]" },
    { k: "track_count", label: "Tracks", className: "w-[7%] text-right" },
    { k: "country", label: "Country", className: "w-[8%]" },
    { k: "catalog_number", label: "Cat #", className: "w-[11%]" },
    { k: "score", label: "Score", className: "w-20 cell-nowrap text-right" },
  ],
  recording: [
    { k: "title", label: "Title" },
    { k: "artist", label: "Artist", className: "w-[26%]" },
    { k: "len", label: "Length", className: "w-[9%]" },
    { k: "first_release_date", label: "First released", className: "w-[13%]" },
    { k: "score", label: "Score", className: "w-20 cell-nowrap text-right" },
  ],
};

export function MBSearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const type = (params.get("type") as MBType) || "release";
  const ptype = params.get("ptype") ?? "";
  const stype = params.get("stype") ?? "";
  const [text, setText] = useState(q);
  const nav = useNavigate();
  const prefetch = useMbPrefetch();

  useEffect(() => setText(q), [q]); // stay in sync with back/forward

  const pushParams = (nextQ: string) => {
    const next = new URLSearchParams();
    if (nextQ.trim()) next.set("q", nextQ.trim());
    next.set("type", type);
    if (ptype) next.set("ptype", ptype);
    if (stype) next.set("stype", stype);
    setParams(next, { replace: true });
  };

  const setTypeFilter = (key: "ptype" | "stype", value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  // Debounced URL sync so every keystroke doesn't fire a request.
  useEffect(() => {
    if (text === q) return;
    const t = setTimeout(() => pushParams(text), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  // A pasted musicbrainz.org link or bare MBID routes to its entity page —
  // bare IDs are type-probed server-side so the user never picks one.
  const urlMatch = MB_URL_RE.exec(q);
  const bareId = !urlMatch && MBID_RE.test(q.trim()) ? q.trim() : null;
  const detect = useQuery({
    queryKey: ["mbIdentify", bareId],
    queryFn: () => api.mbIdentify(bareId!),
    enabled: !!bareId,
    retry: false,
  });
  useEffect(() => {
    if (urlMatch) nav(`/mb/${routeFor(urlMatch[1])}/${urlMatch[2]}`);
  }, [urlMatch?.[1], urlMatch?.[2]]);
  useEffect(() => {
    if (detect.data) nav(`/mb/${routeFor(detect.data.type)}/${detect.data.id}`);
  }, [detect.data]);

  // Catalog numbers and barcodes ("SRCS 8757") often don't rank in a free
  // text search — when the query looks like one, run the exact catno/barcode
  // search instead and fall back to free text only if it comes up empty.
  const looksCatno = /^[a-z0-9]{1,8}[\s-]?\d{3,8}([-]?\d{1,4})?$/i.test(q.trim());
  const looksBarcode = /^\d{8,14}$/.test(q.trim());
  const mode = type === "release" ? (looksBarcode ? "barcode" : looksCatno ? "catno" : "free") : "free";
  const idLike = !!urlMatch || !!bareId;
  const search = useInfiniteQuery({
    queryKey: ["mbSearch", type, q, mode, ptype, stype],
    queryFn: async ({ pageParam }) => {
      const page = await api.mbSearch(type, q, PAGE, mode, pageParam as number, ptype, stype);
      if (mode === "free" || page.rows.length) return page;
      // exact search found nothing at this offset — show the free-text list
      return api.mbSearch(type, q, PAGE, "free", pageParam as number, ptype, stype);
    },
    initialPageParam: 0,
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((n, p) => n + p.rows.length, 0);
      return loaded < (last.total ?? 0) ? loaded : undefined;
    },
    enabled: q.trim().length >= 2 && !idLike,
    placeholderData: keepPreviousData, // keep rows visible while re-querying
  });

  const rows = (search.data?.pages ?? []).flatMap((p) => p.rows);
  const total = search.data?.pages.at(-1)?.total ?? 0;

  // Flatten entity-specific shapes into sortable flat rows for the columns.
  const shaped = useMemo(
    () =>
      rows.map((r: any) => {
        if (type === "artist")
          return {
            ...r,
            life: (r.life_span ?? []).filter(Boolean).join(" – "),
            tags: (r.tags ?? []).join(" · "),
          };
        if (type === "release-group")
          return { ...r, typeLabel: typeLabel(r.primary_type, r.secondary_types) };
        if (type === "recording") return { ...r, len: r.length ? fmtLen(Number(r.length)) : "" };
        return { ...r, typeLabel: typeLabel(r.primary_type, r.secondary_types) };
      }),
    [rows, type]
  );
  const { sort, onSort, sorted } = useSort(shaped, null);

  const renderCells = (r: any) => {
    switch (type) {
      case "artist":
        return (
          <>
            <td className="td">
              <span className="font-medium text-zinc-100">{r.title}</span>
              {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
            </td>
            <td className="td text-zinc-500">{r.type || "—"}</td>
            <td className="td text-zinc-500">{r.country || "—"}</td>
            <td className="td text-zinc-500">{r.life || "—"}</td>
            <td className="td text-zinc-500 truncate">{r.tags || "—"}</td>
            <td className="td text-right font-mono text-[10px] text-zinc-600">{r.score ?? ""}</td>
          </>
        );
      case "release-group":
        return (
          <>
            <td className="td">
              <span className="font-medium text-zinc-100">{r.title}</span>
              {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
            </td>
            <td className="td text-zinc-400 truncate">{r.artist || "—"}</td>
            <td className="td text-zinc-500">{r.typeLabel || "—"}</td>
            <td className="td text-zinc-500">{r.first_release_date || "—"}</td>
            <td className="td text-right font-mono text-[10px] text-zinc-600">{r.score ?? ""}</td>
          </>
        );
      case "release":
        return (
          <>
            <td className="td">
              <span className="font-medium text-zinc-100">{r.title}</span>
              {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
            </td>
            <td className="td text-zinc-400 truncate">{r.artist || "—"}</td>
            <td className="td text-zinc-500 truncate">{r.typeLabel || "—"}</td>
            <td className="td text-zinc-500">{r.date || "—"}</td>
            <td className="td text-zinc-500">{r.formats || "—"}</td>
            <td className="td text-zinc-500 text-right">{r.track_count || "—"}</td>
            <td className="td text-zinc-500">{r.country || "—"}</td>
            <td className="td text-zinc-500 truncate">{r.catalog_number || "—"}</td>
            <td className="td text-right font-mono text-[10px] text-zinc-600">{r.score ?? ""}</td>
          </>
        );
      default:
        return (
          <>
            <td className="td">
              <span className="font-medium text-zinc-100">{r.title}</span>
              {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
            </td>
            <td className="td text-zinc-400 truncate">{r.artist || "—"}</td>
            <td className="td text-zinc-500 font-mono">{r.len || "—"}</td>
            <td className="td text-zinc-500">{r.first_release_date || "—"}</td>
            <td className="td text-right font-mono text-[10px] text-zinc-600">{r.score ?? ""}</td>
          </>
        );
    }
  };

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
        <Zap className="h-6 w-6 text-accent" /> MusicBrainz
      </h1>
      <div className="relative mt-3">
        <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
        <input
          className="input !pl-10"
          placeholder="Search artists, releases, recordings… — or paste an MB ID / link"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter is an explicit search — skip the typing debounce
            if (e.key === "Enter") pushParams(text);
          }}
          autoFocus
        />
      </div>
      <div className="mt-3">
        <Segmented value={type} onChange={(t) => {
          const next = new URLSearchParams(params);
          next.set("type", t);
          setParams(next, { replace: true });
        }} options={TYPES} />
      </div>

      {/* Release types are two axes in MusicBrainz: a primary type (Album /
          Single / EP / …) and any number of secondary types (Soundtrack /
          Live / Compilation / …) — both narrow the search server-side. */}
      {(type === "release" || type === "release-group") && (
        <div className="mt-2 flex items-center gap-2 flex-wrap text-xs">
          <span className="text-[10px] uppercase tracking-widest text-zinc-500">Type</span>
          <select
            className="input !py-1 !w-auto"
            value={ptype}
            onChange={(e) => setTypeFilter("ptype", e.target.value)}
            title="MusicBrainz primary release type"
          >
            <option value="">Any</option>
            {PRIMARY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <span className="text-zinc-600">+</span>
          <select
            className="input !py-1 !w-auto"
            value={stype}
            onChange={(e) => setTypeFilter("stype", e.target.value)}
            title="MusicBrainz secondary release type"
          >
            <option value="">Any</option>
            {SECONDARY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          {(ptype || stype) && (
            <button className="btn-ghost !py-1 text-[11px]" onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("ptype");
              next.delete("stype");
              setParams(next, { replace: true });
            }}>
              Clear
            </button>
          )}
        </div>
      )}

      <div className="mt-4">
        {urlMatch ? (
          <Spinner />
        ) : bareId ? (
          detect.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-24 text-sm text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Looking up {bareId}…
            </div>
          ) : detect.error ? (
            <EmptyState title="No MusicBrainz entity found" hint={`Nothing lives at ${bareId}.`} />
          ) : null
        ) : !q || q.trim().length < 2 ? (
          <EmptyState title="Type at least two characters" hint="Results come straight from musicbrainz.org (rate-limited to 1 request/second — repeated views are cached and pages prefetch on hover)." />
        ) : search.isLoading || (search.isPlaceholderData && !rows.length) ? (
          <Spinner />
        ) : search.error ? (
          <LoadError e={search.error} />
        ) : rows.length === 0 ? (
          <EmptyState title="No results" hint={`Nothing on MusicBrainz for “${q}”.`} />
        ) : (
          <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${search.isPlaceholderData ? "opacity-50" : ""}`}>
            <table className="w-full text-sm">
              <thead className="border-b border-border">
                <tr>
                  {COLUMNS[type].map((c) => (
                    <SortTh key={c.k} label={c.label} k={c.k} sort={sort} onSort={onSort} className={c.className} />
                  ))}
                  <th className="th w-10"></th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((r: any) => (
                  <tr
                    key={String(r.id)}
                    className="table-row !cursor-pointer"
                    onClick={() => nav(`/mb/${routeFor(type)}/${r.id}`)}
                    onMouseEnter={() => prefetch(type, String(r.id))}
                  >
                    {renderCells(r)}
                    <td className="td w-10 pr-2">
                      <ExtLink href={mbUrl(type, String(r.id))} title="Open on MusicBrainz" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <LoadMore
              loaded={rows.length}
              total={total}
              busy={search.isFetchingNextPage}
              onLoad={() => search.fetchNextPage()}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Artist                                                              */
/* ------------------------------------------------------------------ */

export function MBArtistPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const prefetch = useMbPrefetch();
  const discography = useInfiniteQuery({
    queryKey: ["mbArtist", id],
    queryFn: ({ pageParam }) => api.mbArtist(id, pageParam as number),
    initialPageParam: 0,
    getNextPageParam: (last: any, all: any[]) => {
      const loaded = all.reduce((n, p) => n + (p.release_groups?.length ?? 0), 0);
      return loaded < (last.total ?? 0) ? loaded : undefined;
    },
    enabled: !!id,
    placeholderData: keepPreviousData,
  });
  const { isLoading, error } = discography;
  const [typeFilter, setTypeFilter] = useState<string>("All");
  useEffect(() => setTypeFilter("All"), [id]);

  const groups: RGRow[] = (discography.data?.pages ?? []).flatMap((p: any) => p.release_groups ?? []);
  const rgTotal: number = discography.data?.pages.at(-1)?.total ?? groups.length;

  // Release TYPE grouping: primary type splits the sections (Album / EP /
  // Single / …); secondary types (Compilation, Live, …) keep an edition in
  // its own combined category instead of vanishing into "Album".
  const primaryOf = (rg: RGRow) => rg.primary_type || "Other";
  const catOf = (rg: RGRow) =>
    [rg.primary_type || "Other", ...(rg.secondary_types ?? [])].join(" + ");

  const primaries = useMemo(() => {
    const counts = new Map<string, number>();
    for (const rg of groups) {
      const p = primaryOf(rg);
      counts.set(p, (counts.get(p) ?? 0) + 1);
    }
    return [...counts.entries()].sort((x, y) => y[1] - x[1]);
  }, [groups]);
  const shown = typeFilter === "All" ? groups : groups.filter((rg) => primaryOf(rg) === typeFilter);

  const byCat: Record<string, RGRow[]> = {};
  for (const rg of shown) (byCat[catOf(rg)] ??= []).push(rg);

  if (!id) return null;
  if (isLoading) return <Spinner />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  const a = discography.data?.pages[0];
  if (!a) return null;
  const life = (a.life_span ?? []).filter(Boolean).join(" – ");

  return (
    <div>
      <PageHeader
        overline="MusicBrainz artist"
        title={a.name}
        meta={[a.disambiguation, a.type, a.country, life].filter(Boolean).join(" · ")}
        chips={[...(a.genres ?? []), ...(a.tags ?? []).slice(0, 5)].slice(0, 8)}
        mbHref={mbUrl("artist", a.id)}
      />
      <div className="px-6 pb-8 max-w-4xl mx-auto">
        {groups.length === 0 ? (
          <EmptyState title="No release groups on MusicBrainz" />
        ) : (
          <>
            <div className="flex gap-1 flex-wrap mb-4">
              {["All", ...primaries.map(([p]) => p)].map((p) => (
                <button
                  key={p}
                  className={`chip px-2.5 py-1 border ${
                    typeFilter === p
                      ? "bg-accent on-accent border-transparent font-semibold"
                      : "bg-raise border-border text-zinc-400 hover:text-white"
                  }`}
                  onClick={() => setTypeFilter(p)}
                >
                  {p}
                  {p === "All" ? ` (${groups.length})` : ` (${primaries.find(([x]) => x === p)?.[1] ?? 0})`}
                </button>
              ))}
            </div>
            {groups.length !== shown.length && (
              <div className="text-[11px] text-zinc-500 mb-3">
                Showing {shown.length} of {groups.length} release groups
              </div>
            )}
            {Object.entries(byCat).map(([cat, list]) => (
              <div key={cat} className="mb-5">
                <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
                  {cat} · {list.length}
                </div>
                <div className="rounded-lg border border-border overflow-hidden">
                  {list.map((rg) => (
                    <div
                      key={rg.id}
                      className="table-row !cursor-pointer"
                      onClick={() => nav(`/mb/rg/${rg.id}`)}
                      onMouseEnter={() => prefetch("release-group", rg.id)}
                    >
                      <div className="px-3 py-2 flex items-center gap-3 min-w-0">
                        <span className="text-xs font-mono text-zinc-500 w-10 shrink-0">
                          {(rg.first_release_date || "—").slice(0, 4)}
                        </span>
                        <span className="text-sm text-zinc-200 truncate flex-1">
                          {rg.title}
                          {rg.secondary_types?.length ? (
                            <span className="text-zinc-500 text-xs"> ({rg.secondary_types.join(" + ")})</span>
                          ) : null}
                        </span>
                        <ExtLink href={mbUrl("release-group", rg.id)} title="Open on MusicBrainz" />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            <LoadMore
              loaded={groups.length}
              total={rgTotal}
              busy={discography.isFetchingNextPage}
              onLoad={() => discography.fetchNextPage()}
            />
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Release group                                                       */
/* ------------------------------------------------------------------ */

export function MBReleaseGroupPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const prefetch = useMbPrefetch();
  const editions = useInfiniteQuery({
    queryKey: ["mbRG", id],
    queryFn: ({ pageParam }) => api.mbReleaseGroup(id, pageParam as number),
    initialPageParam: 0,
    getNextPageParam: (last: any, all: any[]) => {
      const loaded = all.reduce((n, p) => n + (p.releases?.length ?? 0), 0);
      return loaded < (last.total ?? 0) ? loaded : undefined;
    },
    enabled: !!id,
    placeholderData: keepPreviousData,
  });
  const { isLoading, error } = editions;
  const releasesAll: RelRow[] = (editions.data?.pages ?? []).flatMap((p: any) => p.releases ?? []);
  const relTotal: number = editions.data?.pages.at(-1)?.total ?? releasesAll.length;
  const { sort, onSort, sorted } = useSort(releasesAll, "date");

  if (!id) return null;
  if (isLoading) return <Spinner />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  const rg = editions.data?.pages[0];
  if (!rg) return null;
  const typeLabel = [rg.primary_type, ...(rg.secondary_types ?? [])].filter(Boolean).join(" + ");

  return (
    <div>
      <PageHeader
        overline="MusicBrainz release group"
        title={rg.title}
        meta={[
          rg.artist,
          typeLabel,
          rg.first_release_date,
          rg.disambiguation,
        ]
          .filter(Boolean)
          .join(" · ")}
        chips={rg.genres ?? []}
        mbHref={mbUrl("release-group", rg.id)}
      >
        {rg.artist_mbid && (
          <Link className="btn-ghost !py-1.5 text-xs" to={`/mb/artist/${rg.artist_mbid}`}>
            Artist page
          </Link>
        )}
      </PageHeader>
      <div className="px-6 pb-8 max-w-5xl mx-auto">
        <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
          Releases{releasesAll.length < relTotal ? ` · ${releasesAll.length} of ${relTotal}` : ` · ${relTotal}`}
        </div>
        <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${editions.isPlaceholderData ? "opacity-50" : ""}`}>
          <table className="w-full text-sm">
            <thead className="border-b border-border">
              <tr>
                <SortTh label="Date" k="date" sort={sort} onSort={onSort} className="w-24 cell-nowrap" />
                <SortTh label="Title" k="title" sort={sort} onSort={onSort} />
                <th className="th w-[11%]">Format</th>
                <SortTh label="Discs" k="disc_count" sort={sort} onSort={onSort} className="w-[8%] cell-nowrap text-right" />
                <SortTh label="Tracks" k="track_count" sort={sort} onSort={onSort} className="w-[12%] text-right" />
                <th className="th w-[7%]">Country</th>
                <th className="th w-[9%]">Status</th>
                <th className="th w-[14%]">Barcode</th>
                <th className="th w-10"></th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => (
                <tr
                  key={r.id}
                  className="table-row !cursor-pointer"
                  onClick={() => nav(`/mb/release/${r.id}`)}
                  onMouseEnter={() => prefetch("release", r.id)}
                  title="Open this release"
                >
                  <td className="td text-zinc-500 cell-nowrap">{r.date || "—"}</td>
                  <td className="td text-zinc-200">
                    <span className="truncate">{r.title}</span>
                    {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
                  </td>
                  <td className="td text-zinc-500">{r.formats || "—"}</td>
                  <td className="td text-zinc-500 text-right">{r.disc_count || "—"}</td>
                  <td
                    className="td text-zinc-500 text-right tabular-nums cell-nowrap"
                    title={(r.disc_count ?? 1) > 1 ? `${r.track_count} tracks across ${r.disc_count} discs` : undefined}
                  >
                    {tracksLabel(r) || "—"}
                  </td>
                  <td className="td text-zinc-500">{r.country || "—"}</td>
                  <td className="td text-zinc-500">{r.status || "—"}</td>
                  <td className="td text-zinc-600 font-mono text-[11px] truncate">{r.barcode || ""}</td>
                  <td className="td w-10 pr-2">
                    <ExtLink href={mbUrl("release", r.id)} title="Open on MusicBrainz" />
                  </td>
                </tr>
              ))}
              {!releasesAll.length && (
                <tr>
                  <td colSpan={9} className="p-0">
                    <EmptyState title="No releases in this group" />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <LoadMore
            loaded={releasesAll.length}
            total={relTotal}
            busy={editions.isFetchingNextPage}
            onLoad={() => editions.fetchNextPage()}
          />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Release                                                             */
/* ------------------------------------------------------------------ */

export function MBReleasePage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { data: r, isLoading, error } = useQuery({
    queryKey: ["mbRelease", id],
    queryFn: () => api.mbRelease(id),
    enabled: !!id,
  });
  const [wished, setWished] = useState(false);
  const [wishBusy, setWishBusy] = useState(false);
  if (isLoading) return <Spinner />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  if (!r) return null;

  const cover = `https://coverartarchive.org/release/${r.id}/front-500`;
  const artist = r.artists?.map((a) => a.name).join(", ") || "";
  const discs: Record<number, MBTrackRow[]> = {};
  for (const t of (r.media ?? []) as MBTrackRow[]) {
    (discs[t.disc] ??= []).push(t);
  }

  return (
    <div>
      <PageHeader
        overline="MusicBrainz release"
        title={r.title}
        soulseekQuery={[r.artists?.[0]?.name, r.title].filter(Boolean).join(" ")}
        meta={[
          artist,
          typeLabel(r.primary_type, r.secondary_types) || r.release_type,
          r.date,
          [r.label, r.catalog_number].filter(Boolean).join(" · "),
          r.country,
          r.barcode,
        ]
          .filter(Boolean)
          .join(" · ")}
        chips={r.genres ?? []}
        mbHref={mbUrl("release", r.id)}
      >
        <button
          className="btn-ghost !py-1.5 text-xs"
          title="Save this release to the wishlist — it is auto-imported from Soulseek when a verified copy appears"
          disabled={wishBusy || wished}
          onClick={async () => {
            setWishBusy(true);
            try {
              await api.wishAdd({
                release_mbid: r.id,
                title: r.title,
                artist,
                year: (r.date || "").slice(0, 4),
              });
              setWished(true);
              toast("Added to wishes");
            } catch (e) {
              toast(String(e));
            } finally {
              setWishBusy(false);
            }
          }}
        >
          {wished ? <Check className="h-3.5 w-3.5" /> : <BookmarkPlus className="h-3.5 w-3.5" />}
          {wished ? "Wished" : "Add to wishes"}
        </button>
        <button
          className="btn-primary !py-1.5 text-xs"
          title="Find → verify → download → audit → import this exact release from Soulseek"
          onClick={async () => {
            try {
              await api.soulseekAutoStart({ release_mbid: r.id });
              toast("Auto-import started");
              nav(`/soulseek?release=${encodeURIComponent(r.id)}`);
            } catch (e) {
              toast(String(e));
            }
          }}
        >
          <Zap className="h-3.5 w-3.5" /> Auto-import
        </button>
        {r.release_group_id && (
          <Link className="btn-ghost !py-1.5 text-xs" to={`/mb/rg/${r.release_group_id}`}>
            Release group
          </Link>
        )}
        <a className="btn-ghost !py-1.5 text-xs" href={cover} target="_blank" rel="noreferrer" title="Cover Art Archive">
          Cover art
        </a>
      </PageHeader>

      <div className="px-6 pb-8 max-w-4xl mx-auto grid grid-cols-1 lg:grid-cols-[240px_1fr] gap-6">
        <div>
          <img
            src={cover}
            alt=""
            className="w-full rounded-lg border border-border bg-raise"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.visibility = "hidden";
            }}
          />
          <div className="text-[10px] text-zinc-600 mt-1.5">
            Artwork from the Cover Art Archive — download it into a local album via the album page's “Find cover online”.
          </div>
        </div>
        <div className="space-y-4">
          {Object.entries(discs).map(([disc, tracks]) => (
            <div key={disc}>
              <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
                Disc {disc}{r.medium_formats?.[Number(disc) - 1] ? ` · ${r.medium_formats[Number(disc) - 1]}` : ""}
              </div>
              <div className="rounded-lg border border-border overflow-hidden">
                {tracks.map((t) => (
                  <div key={`${t.disc}-${t.position}`} className="flex items-center gap-3 px-3 py-1.5 border-t border-border/60 first:border-t-0">
                    <span className="text-[11px] font-mono text-zinc-600 w-6 text-right shrink-0">
                      {t.position}
                    </span>
                    <div className="flex-1 min-w-0">
                      <span className="text-sm text-zinc-200 truncate">{t.title}</span>
                      {t.artist_credit && t.artist_credit !== artist && (
                        <span className="text-[11px] text-zinc-500"> — {t.artist_credit}</span>
                      )}
                    </div>
                    <span className="text-[11px] font-mono text-zinc-500 shrink-0">{fmtLen(t.length)}</span>
                    {t.recording_mbid && (
                      <Link
                        to={`/mb/recording/${t.recording_mbid}`}
                        className="p-1 rounded-lg text-zinc-600 hover:text-white hover:bg-raise transition-colors shrink-0"
                        title="Open this track on MusicBrainz"
                      >
                        <ArrowUpRight className="h-3.5 w-3.5" />
                      </Link>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Recording ("track")                                                 */
/* ------------------------------------------------------------------ */

export function MBRecordingPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const prefetch = useMbPrefetch();
  const appearances = useInfiniteQuery({
    queryKey: ["mbRecording", id],
    queryFn: ({ pageParam }) => api.mbRecording(id, pageParam as number),
    initialPageParam: 0,
    getNextPageParam: (last: any, all: any[]) => {
      const loaded = all.reduce((n, p) => n + (p.releases?.length ?? 0), 0);
      return loaded < (last.total ?? 0) ? loaded : undefined;
    },
    enabled: !!id,
    placeholderData: keepPreviousData,
  });
  const { isLoading, error } = appearances;
  const releasesAll: RelRow[] = (appearances.data?.pages ?? []).flatMap((p: any) => p.releases ?? []);
  const relTotal: number = appearances.data?.pages.at(-1)?.total ?? releasesAll.length;
  const { sort, onSort, sorted } = useSort(releasesAll, "date");

  if (!id) return null;
  if (isLoading) return <Spinner />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  const r = appearances.data?.pages[0];
  if (!r) return null;

  return (
    <div>
      <PageHeader
        overline="MusicBrainz recording"
        title={r.title}
        soulseekQuery={[r.artist, r.title].filter(Boolean).join(" ")}
        meta={[
          r.artist,
          r.length ? fmtLen(r.length) : "",
          r.disambiguation,
          r.isrcs?.length ? `ISRC ${r.isrcs.join(", ")}` : "",
        ]
          .filter(Boolean)
          .join(" · ")}
        chips={r.genres ?? []}
        mbHref={mbUrl("recording", r.id)}
      >
        {r.artist_mbid && (
          <Link className="btn-ghost !py-1.5 text-xs" to={`/mb/artist/${r.artist_mbid}`}>
            Artist page
          </Link>
        )}
      </PageHeader>
      <div className="px-6 pb-8 max-w-5xl mx-auto">
        <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
          Appears on{releasesAll.length < relTotal ? ` · ${releasesAll.length} of ${relTotal} releases` : ` · ${relTotal} releases`}
        </div>
        <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${appearances.isPlaceholderData ? "opacity-50" : ""}`}>
          <table className="w-full text-sm">
            <thead className="border-b border-border">
              <tr>
                <SortTh label="Date" k="date" sort={sort} onSort={onSort} className="w-24 cell-nowrap" />
                <SortTh label="Title" k="title" sort={sort} onSort={onSort} />
                <th className="th w-[10%]">Format</th>
                <th className="th w-[10%]">Type</th>
                <SortTh label="Tracks" k="track_count" sort={sort} onSort={onSort} className="w-[11%] text-right" />
                <th className="th w-[8%]">Country</th>
                <th className="th w-10"></th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((rel) => (
                <tr
                  key={rel.id}
                  className="table-row !cursor-pointer"
                  onClick={() => nav(`/mb/release/${rel.id}`)}
                  onMouseEnter={() => prefetch("release", rel.id)}
                >
                  <td className="td text-zinc-500 cell-nowrap">{rel.date || "—"}</td>
                  <td className="td text-zinc-200">
                    <span className="truncate">{rel.title}</span>
                  </td>
                  <td className="td text-zinc-500">{rel.formats || "—"}</td>
                  <td className="td text-zinc-500 truncate">
                    {typeLabel(rel.primary_type, rel.secondary_types) || "—"}
                  </td>
                  <td className="td text-zinc-500 text-right tabular-nums cell-nowrap">{tracksLabel(rel) || "—"}</td>
                  <td className="td text-zinc-500">{rel.country || "—"}</td>
                  <td className="td w-10 pr-2">
                    <ExtLink href={mbUrl("release", rel.id)} title="Open on MusicBrainz" />
                  </td>
                </tr>
              ))}
              {!releasesAll.length && (
                <tr>
                  <td colSpan={7} className="p-0">
                    <EmptyState title="No releases carry this recording" />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <LoadMore
            loaded={releasesAll.length}
            total={relTotal}
            busy={appearances.isFetchingNextPage}
            onLoad={() => appearances.fetchNextPage()}
          />
        </div>
      </div>
    </div>
  );
}
