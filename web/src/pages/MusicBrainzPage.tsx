import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient,
} from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ArrowUpDown, ArrowUpRight, BookmarkPlus, Check, Loader2, Search, X, Zap } from "lucide-react";
import { api } from "../api";
import type { DiscoveryAlbumDetail, DiscoveryRow, MBRelease } from "../types";
import { EmptyState, PageLoading } from "../components/Badges";
import { MbIcon } from "../components/Links";
import PageHeader from "../components/PageHeader";
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

/** Which chain answers the search box (config `mb_search_source`). */
const SEARCH_MODES = [
  { id: "auto", label: "Auto" },
  { id: "discovery", label: "Discovery" },
  { id: "musicbrainz", label: "MusicBrainz" },
] as const;
type SearchMode = (typeof SEARCH_MODES)[number]["id"];

/** Provider id → short label for the "who answered" caption. Mirrors
 *  mlo.discovery.SOURCE_LABELS for the providers a search can come from. */
const PROVIDER_LABELS: Record<string, string> = {
  deezer: "Deezer", itunes: "iTunes", listenbrainz: "ListenBrainz",
  audiodb: "TheAudioDB", wikipedia: "Wikipedia", musicbrainz: "MusicBrainz",
};

const DISCOVERY_LIMIT = 50;

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
/** The shared, entity-agnostic fields a search row can carry (each entity
 *  adds its own: artist tags, release formats, release-group types …). */
interface MBSearchRow {
  id: string;
  score?: number;
  title?: string;
  disambiguation?: string;
  artist?: string;
  artist_mbid?: string;
  artists?: { name?: string; mbid?: string }[];
  status?: string;
  formats?: string;
  primary_type?: string;
  secondary_types?: string[];
  release_type?: string;
  catalog_number?: string;
  track_count?: number;
  country?: string;
  date?: string;
  first_release_date?: string;
  type?: string;
  life?: string;
  tags?: string;
  len?: string;
  typeLabel?: string;
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

/** Anything MusicBrainz marks beyond a normal commercial pressing is worth
 *  flagging before a download: Promotion → "Promo" (amber), the rest
 *  (Bootleg / Pseudo-Release / Withdrawn / …) red. The format string rides
 *  along as a neutral chip, so a row reads "2×CD Promo" at a glance. */
function StatusBadge({ status, formats }: { status?: string | null; formats?: string | null }) {
  const s = (status || "").trim();
  const flagged = !!s && s.toLowerCase() !== "official";
  return (
    <>
      {formats ? (
        <span className="chip bg-raise border border-border text-zinc-400">{formats}</span>
      ) : null}
      {flagged ? (
        <span
          className={`chip border ${
            /promo/i.test(s)
              ? "bg-amber-500/15 text-amber-300 border-amber-600/40"
              : "bg-red-500/15 text-red-300 border-red-800/60"
          }`}
          title={`MusicBrainz release status: ${s}`}
        >
          {/^promotion$/i.test(s) ? "Promo" : s}
        </span>
      ) : null}
    </>
  );
}

/** "best" = the one edition the auto-import policy prefers per release group
 *  (default); "all" = every eligible edition. Sent to /api/mb/auto-import. */
const IMPORT_MODES = [
  { id: "best", label: "Best edition" },
  { id: "all", label: "All editions" },
] as const;
type ImportMode = (typeof IMPORT_MODES)[number]["id"];

/** The two actions every entity header carries: the MusicBrainz link and the
 *  Soulseek handoff (which searches the artist + title). */
function MbHeaderActions({ href, query }: { href?: string; query: string }) {
  const nav = useNavigate();
  return (
    <>
      {href ? <ExtLink href={href} title="Open on MusicBrainz" /> : null}
      <button
        className="btn-ghost !py-1.5 text-xs"
        title="Search Soulseek for this"
        onClick={() => nav(`/soulseek?q=${encodeURIComponent(query)}`)}
      >
        <Search className="h-3.5 w-3.5" /> Soulseek
      </button>
    </>
  );
}

/** Bulk auto-import with a busy flag: queues one server-side job per id and
 *  reports what the server actually queued (`queued` / `skipped`), never what
 *  was asked for. `missing` is ids that had nothing to send (already counted
 *  as skipped). */
function useAutoImport() {
  const [busy, setBusy] = useState(false);
  const run = async (
    ids: string[],
    kind: "release" | "release_group" | "artist",
    mode: ImportMode,
    missing = 0
  ) => {
    setBusy(true);
    let queued = 0;
    let skipped = missing;
    try {
      for (const mbid of ids) {
        try {
          const res = await api.mbAutoImport({ mbid, kind, mode });
          queued += res.queued;
          skipped += res.skipped.length;
        } catch {
          skipped += 1; // one bad id must not abandon the rest of the batch
        }
      }
    } finally {
      setBusy(false);
    }
    const msg = `${queued} queued${skipped ? ` · ${skipped} skipped` : ""}`;
    if (queued) toast.success(msg);
    else toast.error(`Nothing queued${skipped ? ` · ${skipped} skipped` : ""}`);
  };
  return { busy, run };
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

/* ------------------------------------------------------------------ */
/* Discovery results — the provider catalogue chain behind the search  */
/* ------------------------------------------------------------------ */

/** Loading state in the shape of the result grid (same boxes the library
 *  page uses while scanning), so nothing jumps when rows land. */
function ResultSkeleton() {
  return (
    <div
      className="grid gap-x-4 gap-y-5 animate-pulse"
      style={{ gridTemplateColumns: "repeat(auto-fill, minmax(164px, 1fr))" }}
      aria-busy="true"
    >
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="p-2">
          <div className="aspect-square w-full rounded-xl bg-zinc-800/60" />
          <div className="h-3 w-3/4 rounded bg-zinc-800/60 mt-2.5" />
          <div className="h-2.5 w-1/2 rounded bg-zinc-800/40 mt-1.5" />
        </div>
      ))}
    </div>
  );
}

/** One discovery row: provider cover art, title/artist, year, type chips and
 *  the provider's own popularity label. A row carrying an MBID drills into its
 *  MusicBrainz entity (the download path); a Deezer row without one opens the
 *  in-page panel, which resolves the MBID on demand. */
function DiscoveryCard({ row, onDrill, onOpen }: {
  row: DiscoveryRow;
  onDrill: (path: string) => void;
  onOpen: (row: DiscoveryRow) => void;
}) {
  const title = row.title || row.name || "";
  const cover = row.cover ?? row.image ?? null;
  // Artist rows have no detail endpoint (only albums do), and their
  // `deezer_id` is an ARTIST id — never feed it to the album lookup.
  const subtitle = row.kind === "artist"
    ? [row.disambiguation, row.country, row.genre].filter(Boolean).join(" · ")
    : row.artist || row.similar_to || "";
  const mbPath = row.mbid
    ? row.kind === "artist" ? `/mb/artist/${row.mbid}` : `/mb/rg/${row.mbid}`
    : null;
  const open = mbPath ? () => onDrill(mbPath)
    : row.kind === "album" && row.deezer_id ? () => onOpen(row)
    : null;
  const kinds = [row.record_type, ...(row.secondary_types ?? []), row.genre].filter(Boolean) as string[];
  return (
    <div
      role={open ? "button" : undefined}
      tabIndex={open ? 0 : undefined}
      onClick={open ?? undefined}
      onKeyDown={open ? (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      } : undefined}
      className={`group flex flex-col rounded-lg border border-border bg-card overflow-hidden transition-colors ${
        open ? "cursor-pointer hover:border-accent/60" : ""
      }`}
    >
      <div className="aspect-square w-full bg-panel overflow-hidden">
        {cover ? (
          <img
            src={cover}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover transition-transform duration-200 group-hover:scale-[1.03]"
          />
        ) : (
          <div className="h-full w-full grid place-items-center text-[10px] text-zinc-600">no cover</div>
        )}
      </div>
      <div className="p-2.5 flex flex-col gap-1.5">
        <div className="text-sm font-medium text-zinc-100 truncate" title={title}>{title || "Untitled"}</div>
        {subtitle ? <div className="text-xs text-zinc-400 truncate">{subtitle}</div> : null}
        <div className="flex flex-wrap items-center gap-1">
          {row.year ? <span className="chip bg-raise border border-border text-zinc-400">{row.year}</span> : null}
          {kinds.slice(0, 2).map((k) => (
            <span key={k} className="chip bg-raise border border-border text-zinc-400 capitalize">{k}</span>
          ))}
          {row.tracks ? <span className="chip bg-raise border border-border text-zinc-400">{row.tracks} tracks</span> : null}
          {row.popularity_label ? (
            <span className="chip bg-accent/10 text-accent-soft border border-accent/25">{row.popularity_label}</span>
          ) : null}
          {row.owned_path ? (
            <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">owned</span>
          ) : null}
        </div>
        <div className="flex items-center gap-2 text-[10px] text-zinc-500">
          <span>{PROVIDER_LABELS[row.source] ?? row.source}</span>
          <span className={row.mbid ? "text-accent-soft" : "text-zinc-600"}>{row.mbid ? "MBID" : "no MBID"}</span>
          {row.link ? (
            <a
              href={row.link}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="ml-auto hover:text-white"
              title="Open on the provider"
            >
              <ArrowUpRight className="h-3 w-3" />
            </a>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/** In-page detail for a discovery album with no MBID yet: the provider's own
 *  album payload (cover, label, genres, date) and its track list with per-track
 *  popularity, plus the two ways onward — wish it (the backend resolves the
 *  release group) or jump into the resolved MusicBrainz release group. */
function DiscoveryAlbumPanel({ row, album, loading, error, wished, busy, mbid, onWish, onViewMb, onClose }: {
  row: DiscoveryRow;
  album?: DiscoveryAlbumDetail;
  loading: boolean;
  error: unknown;
  wished: boolean;
  busy: boolean;
  mbid: string | null;
  onWish: () => void;
  onViewMb: () => void;
  onClose: () => void;
}) {
  const title = album?.title || row.title || "";
  const artist = album?.artist || row.artist || "";
  const cover = album?.cover ?? row.cover ?? null;
  const kinds = [
    album?.record_type ?? row.record_type,
    ...(album?.secondary_types ?? row.secondary_types ?? []),
  ].filter(Boolean) as string[];
  const tracks = album?.track_list ?? [];
  return (
    <div
      className="fixed inset-0 z-40 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 sm:p-6"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-xl w-full max-w-2xl max-h-[85vh] overflow-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-5 py-3 border-b border-border sticky top-0 bg-card z-10">
          <Zap className="h-4 w-4 text-accent" />
          <span className="font-semibold text-sm truncate">{title}{artist ? ` — ${artist}` : ""}</span>
          <button className="ml-auto p-1 text-zinc-500 hover:text-white" onClick={onClose} title="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading the album from the provider…
            </div>
          ) : error ? (
            <LoadError e={error} />
          ) : (
            <>
              <div className="flex gap-4">
                <div className="w-36 h-36 shrink-0 rounded-lg overflow-hidden bg-panel border border-border">
                  {cover ? <img src={cover} alt="" className="h-full w-full object-cover" /> : null}
                </div>
                <div className="min-w-0 space-y-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    {album?.year ? <span className="chip bg-raise border border-border text-zinc-300">{album.year}</span> : null}
                    {album?.release_date ? (
                      <span className="chip bg-raise border border-border text-zinc-400">{album.release_date}</span>
                    ) : null}
                    {kinds.map((k) => (
                      <span key={k} className="chip bg-raise border border-border text-zinc-400 capitalize">{k}</span>
                    ))}
                    {album?.label ? (
                      <span className="chip bg-raise border border-border text-zinc-400">{album.label}</span>
                    ) : null}
                    {album?.popularity_label ? (
                      <span className="chip bg-accent/10 text-accent-soft border border-accent/25">{album.popularity_label}</span>
                    ) : null}
                  </div>
                  {album?.genres?.length ? (
                    <div className="flex flex-wrap gap-1.5">
                      {album.genres.map((g) => (
                        <span key={g} className="chip bg-white/5 border border-white/15 text-zinc-400">{g}</span>
                      ))}
                    </div>
                  ) : null}
                  <div className="text-[11px] text-zinc-500">
                    {album?.tracks ? `${album.tracks} tracks` : ""}
                    {album?.duration ? ` · ${Math.round(album.duration / 60)} min` : ""}
                    {" · "}
                    {album?.mbid ? "MusicBrainz release group resolved" : "no MusicBrainz release group yet"}
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-2 flex-wrap">
                <button
                  className="btn-primary !py-1.5 text-xs"
                  disabled={busy || wished}
                  onClick={onWish}
                  title="Save this album to the wishlist — it is auto-imported from Soulseek when a verified copy appears"
                >
                  {wished ? <Check className="h-3.5 w-3.5" /> : <BookmarkPlus className="h-3.5 w-3.5" />}
                  {busy ? "Adding…" : wished ? "Wished" : "Add to wishes"}
                </button>
                {mbid ? (
                  <button className="btn-ghost !py-1.5 text-xs" onClick={onViewMb}>
                    <MbIcon className="h-3.5 w-3.5" /> View in MusicBrainz
                  </button>
                ) : null}
                {album?.link ? <ExtLink href={album.link} title="Open on the provider" /> : null}
              </div>

              {tracks.length > 0 && (
                <div className="rounded-lg border border-border overflow-hidden">
                  <table className="w-full text-sm">
                    <thead className="border-b border-border">
                      <tr>
                        <th className="th w-8">#</th>
                        <th className="th">Track</th>
                        <th className="th w-16 text-right">Time</th>
                        <th className="th w-24 text-right">Popularity</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tracks.map((t, i) => (
                        <tr key={`${i}-${t.title}`} className="border-b border-border/60 last:border-0">
                          <td className="td font-mono text-[10px] text-zinc-600">{i + 1}</td>
                          <td className="td text-zinc-200">{t.title || "—"}</td>
                          <td className="td text-right font-mono text-[11px] text-zinc-500">
                            {fmtLen((t.duration ?? 0) * 1000)}
                          </td>
                          <td className="td text-right font-mono text-[10px] text-zinc-500">
                            {t.rank ? t.rank.toLocaleString() : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

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

  const idLike = !!urlMatch || !!bareId;

  // Which chain answers the search box, persisted in the config
  // (`mb_search_source`) so the settings page and the global search read the
  // same value. The segmented control only WRITES — the refetched config is
  // what keys the queries, so a search always runs against the mode the server
  // actually applied.
  const qc = useQueryClient();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const rawSource = config?.mb_search_source;
  const srcMode: SearchMode =
    rawSource === "discovery" || rawSource === "musicbrainz" ? rawSource : "auto";
  const saveSource = useMutation({
    mutationFn: (value: SearchMode) =>
      api.saveConfig({ ...(config ?? {}), mb_search_source: value }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
    onError: (e) => toast.error(String(e)),
  });

  // The discovery chain answers the album ("release-group") and artist tabs;
  // Releases/Recordings exist only on MusicBrainz.
  const discoveryType: "album" | "artist" | null =
    type === "artist" ? "artist" : type === "release-group" ? "album" : null;
  const useDiscovery = !!discoveryType && srcMode !== "musicbrainz" && !idLike;
  const disco = useQuery({
    queryKey: ["discoverySearch", discoveryType, q, srcMode],
    queryFn: () => api.discoverySearch(q, discoveryType!, DISCOVERY_LIMIT),
    enabled: useDiscovery && q.trim().length >= 2,
    placeholderData: keepPreviousData,
  });

  // In-page panel for a row the catalogue knows but MusicBrainz has not been
  // asked about yet — `resolve` is what answers with the release-group MBID.
  // The panel is modal, so the query behind it cannot change while it is open;
  // opening a row resets the wish/resolve state instead of an effect.
  const [detail, setDetail] = useState<DiscoveryRow | null>(null);
  const [wished, setWished] = useState(false);
  const [resolvedMbid, setResolvedMbid] = useState<string | null>(null);
  const openDetail = (row: DiscoveryRow) => {
    setDetail(row);
    setWished(false);
    setResolvedMbid(null);
  };
  const detailId = detail?.deezer_id ?? 0;
  const album = useQuery({
    queryKey: ["discoveryAlbum", detailId],
    queryFn: () => api.discoveryAlbum(detailId, true),
    enabled: detailId > 0,
  });
  const wish = useMutation({
    mutationFn: (row: DiscoveryRow) =>
      api.discoveryWish({
        artist: row.artist ?? "",
        title: row.title ?? "",
        year: row.year ?? "",
        mbid: row.mbid ?? "",
      }),
    onSuccess: (res) => {
      setWished(true);
      setResolvedMbid(res.resolved?.mbid ?? null);
      toast.success("Added to wishes");
    },
    onError: (e) => toast.error(String(e)),
  });

  // Catalog numbers and barcodes ("SRCS 8757") often don't rank in a free
  // text search — when the query looks like one, run the exact catno/barcode
  // search instead and fall back to free text only if it comes up empty.
  const looksCatno = /^[a-z0-9]{1,8}[\s-]?\d{3,8}([-]?\d{1,4})?$/i.test(q.trim());
  const looksBarcode = /^\d{8,14}$/.test(q.trim());
  const mode = type === "release" ? (looksBarcode ? "barcode" : looksCatno ? "catno" : "free") : "free";
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
    enabled: q.trim().length >= 2 && !idLike && !useDiscovery,
    placeholderData: keepPreviousData, // keep rows visible while re-querying
  });

  const rows = (search.data?.pages ?? []).flatMap((p) => p.rows);
  const total = search.data?.pages.at(-1)?.total ?? 0;

  const dro = disco.data?.rows ?? [];
  // Who answered: discovery rows name their providers, MusicBrainz rows (and
  // the forced MusicBrainz mode) are what the chain falls back to. The
  // discovery rows are only trusted while it is the chain answering — the
  // query keeps its previous rows as a placeholder across mode changes.
  const seenProviders: Record<string, true> = {};
  for (const r of dro) seenProviders[r.source] = true;
  const providers = useDiscovery
    ? Object.keys(seenProviders).filter((s) => s !== "musicbrainz")
    : [];
  const answered = providers.length
    ? `${providers.map((s) => PROVIDER_LABELS[s] ?? s).join(" · ")}${
        srcMode === "auto" ? " · MusicBrainz fallback" : " · discovery only"
      }`
    : "MusicBrainz";
  const rgMbid = album.data?.mbid ?? resolvedMbid ?? null;

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

  // Bulk auto-import works off release-group ids, so selection only exists on
  // that tab; it resets whenever the tab or the query changes.
  const [sel, setSel] = useState<string[]>([]);
  const [wishBatchBusy, setWishBatchBusy] = useState(false);
  const { busy: importBusy, run: runImport } = useAutoImport();
  useEffect(() => setSel([]), [type, q]);
  const selectable = type === "release-group";
  const toggleSel = (id: string) =>
    setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  // a row the payload could not identify still counts as selected, so the
  // batch reports it as skipped instead of queueing `undefined`
  const selIds = sel.filter((x) => x && x !== "undefined");

  /** The credited artist, linked to its MusicBrainz page when the row carries
   *  an id — search rows usually do not, so plain text is the common case. */
  const artistCell = (r: MBSearchRow) => {
    const mbid = r.artists?.[0]?.mbid || r.artist_mbid || "";
    const name = r.artist || "—";
    if (!mbid) return <td className="td text-zinc-400 truncate">{name}</td>;
    return (
      <td className="td text-zinc-400 truncate">
        <button
          className="truncate text-left hover:text-accent-soft"
          title="Open the artist on MusicBrainz"
          onClick={(e) => {
            e.stopPropagation(); // the row itself opens the entity page
            nav(`/mb/artist/${mbid}`);
          }}
        >
          {name}
        </button>
      </td>
    );
  };

  const renderCells = (r: MBSearchRow) => {
    const artist = artistCell(r);
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
            {artist}
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
            {artist}
            <td className="td text-zinc-500 truncate">{r.typeLabel || "—"}</td>
            <td className="td text-zinc-500">{r.date || "—"}</td>
            <td className="td text-zinc-500">
              <span className="inline-flex flex-wrap items-center gap-1">
                <StatusBadge status={r.status} formats={r.formats} />
              </span>
            </td>
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
            {artist}
            <td className="td text-zinc-500 font-mono">{r.len || "—"}</td>
            <td className="td text-zinc-500">{r.first_release_date || "—"}</td>
            <td className="td text-right font-mono text-[10px] text-zinc-600">{r.score ?? ""}</td>
          </>
        );
    }
  };

  return (
    <div className="p-6 space-y-5 max-w-6xl mx-auto">
      {/* Sticky, but through the shared primitive: its `top-12` clears the
          floating top bar (a `top-0` header hides underneath it and loses its
          clicks to the bar's search input). */}
      <PageHeader sticky icon={Zap} title="MusicBrainz">
        <div className="relative">
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
        <div className="flex items-center gap-3 flex-wrap">
          <Segmented value={type} onChange={(t) => {
            const next = new URLSearchParams(params);
            next.set("type", t);
            setParams(next, { replace: true });
          }} options={TYPES} />
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-widest text-zinc-500">Source</span>
            <Segmented
              value={srcMode}
              onChange={(m) => saveSource.mutate(m)}
              options={SEARCH_MODES}
              className={saveSource.isPending ? "opacity-60" : ""}
            />
            <span className="text-[10px] text-zinc-600" title="Saved to the config key mb_search_source">
              {srcMode === "auto" ? "discovery first, MusicBrainz behind it"
                : srcMode === "discovery" ? "discovery providers only"
                : "MusicBrainz API only"}
            </span>
          </div>
        </div>

        {/* Release types are two axes in MusicBrainz: a primary type (Album /
            Single / EP / …) and any number of secondary types (Soundtrack /
            Live / Compilation / …) — both narrow the search server-side, so
            they only apply while MusicBrainz is the one answering. */}
        {(type === "release" || type === "release-group") && !useDiscovery && (
          <div className="flex items-center gap-2 flex-wrap text-xs">
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
      </PageHeader>

      <div>
        {urlMatch ? (
          <PageLoading label="Asking MusicBrainz…" />
        ) : bareId ? (
          detect.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-24 text-sm text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Looking up {bareId}…
            </div>
          ) : detect.error ? (
            <EmptyState title="No MusicBrainz entity found" hint={`Nothing lives at ${bareId}.`} />
          ) : null
        ) : !q || q.trim().length < 2 ? (
          <EmptyState
            title="Type at least two characters"
            hint={
              srcMode === "musicbrainz"
                ? "Results come straight from musicbrainz.org (rate-limited to 1 request/second — repeated views are cached and pages prefetch on hover)."
                : "Results come from the discovery providers, with MusicBrainz as the identity source for downloads. Switch the source to MusicBrainz for the raw musicbrainz.org search."
            }
          />
        ) : useDiscovery ? (
          disco.isLoading || (disco.isPlaceholderData && !dro.length) ? (
            <ResultSkeleton />
          ) : disco.error ? (
            <LoadError e={disco.error} />
          ) : dro.length === 0 ? (
            <EmptyState title="No results" hint={`Nothing in the catalogue for “${q}”.`} />
          ) : (
            <>
              <div className="mb-3 flex items-center justify-between gap-3 text-[11px] text-zinc-500">
                <span>{dro.length} result{dro.length === 1 ? "" : "s"}</span>
                <span
                  className="truncate"
                  title={`How this search was answered (config mb_search_source: ${srcMode})`}
                >
                  {answered}
                </span>
              </div>
              <div
                className={`grid gap-x-4 gap-y-5 transition-opacity ${disco.isPlaceholderData ? "opacity-50" : ""}`}
                style={{ gridTemplateColumns: "repeat(auto-fill, minmax(164px, 1fr))" }}
              >
                {dro.map((row, i) => (
                  <DiscoveryCard
                    key={`${row.source}-${row.kind}-${row.deezer_id ?? row.mbid ?? i}`}
                    row={row}
                    onDrill={(path) => nav(path)}
                    onOpen={openDetail}
                  />
                ))}
              </div>
            </>
          )
        ) : search.isLoading || (search.isPlaceholderData && !rows.length) ? (
          <PageLoading label="Asking MusicBrainz…" />
        ) : search.error ? (
          <LoadError e={search.error} />
        ) : rows.length === 0 ? (
          <EmptyState title="No results" hint={`Nothing on MusicBrainz for “${q}”.`} />
        ) : (
          <>
            <div className="mb-3 flex items-center justify-between gap-3 text-[11px] text-zinc-500">
              <span>{rows.length} of {total} result{total === 1 ? "" : "s"} loaded</span>
              <span title={`How this search was answered (config mb_search_source: ${srcMode})`}>{answered}</span>
            </div>
            {selectable && sel.length > 0 && (
              <div className="panel mb-3 flex items-center gap-2 flex-wrap">
                <span className="text-xs font-medium text-accent-soft">
                  {sel.length} release group{sel.length === 1 ? "" : "s"} selected
                </span>
                <div className="ml-auto flex gap-1.5 flex-wrap">
                  <button
                    className="btn-primary !py-1 text-xs"
                    disabled={importBusy}
                    onClick={() => runImport(selIds, "release_group", "best", sel.length - selIds.length)}
                    title="Queue one release per group — the edition the auto-import policy prefers"
                  >
                    <Zap className="h-3.5 w-3.5" /> Auto-import (best per group)
                  </button>
                  <button
                    className="btn-ghost !py-1 text-xs"
                    disabled={importBusy}
                    onClick={() => runImport(selIds, "release_group", "all", sel.length - selIds.length)}
                    title="Queue every eligible edition of each selected group"
                  >
                    Auto-import (all)
                  </button>
                  <button
                    className="btn-ghost !py-1 text-xs"
                    disabled={wishBatchBusy}
                    onClick={async () => {
                      setWishBatchBusy(true);
                      let added = 0;
                      try {
                        for (const release_mbid of selIds) {
                          try {
                            await api.wishAdd({ release_mbid });
                            added += 1;
                          } catch {
                            /* one refused wish must not abandon the batch */
                          }
                        }
                      } finally {
                        setWishBatchBusy(false);
                      }
                      const missed = sel.length - added;
                      toast.success(`Added ${added} to wishes${missed ? ` · ${missed} skipped` : ""}`);
                      setSel([]);
                    }}
                  >
                    <BookmarkPlus className="h-3.5 w-3.5" /> Add to wishes
                  </button>
                  <button className="btn-ghost !py-1 text-xs" onClick={() => setSel([])}>
                    Clear
                  </button>
                </div>
              </div>
            )}
            <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${search.isPlaceholderData ? "opacity-50" : ""}`}>
              <div className="table-scroll">
                <table className="w-full text-sm">
                  <thead className="border-b border-border">
                    <tr>
                      {selectable && (
                        <th className="th w-8">
                          <input
                            type="checkbox"
                            title="Select every loaded result"
                            checked={sorted.length > 0 && sel.length === sorted.length}
                            onChange={() =>
                              setSel(
                                sel.length === sorted.length
                                  ? []
                                  : sorted.map((r: MBSearchRow) => String(r.id)).filter(Boolean)
                              )
                            }
                          />
                        </th>
                      )}
                      {COLUMNS[type].map((c) => (
                        <SortTh key={c.k} label={c.label} k={c.k} sort={sort} onSort={onSort} className={c.className} />
                      ))}
                      <th className="th w-16"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {sorted.map((r: MBSearchRow) => (
                      <tr
                        key={String(r.id)}
                        className={`table-row !cursor-pointer ${sel.includes(String(r.id)) ? "bg-accent/15" : ""}`}
                        onClick={() => nav(`/mb/${routeFor(type)}/${r.id}`)}
                        onMouseEnter={() => prefetch(type, String(r.id))}
                      >
                        {selectable && (
                          <td className="td w-8 pr-0" onClick={(e) => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              checked={sel.includes(String(r.id))}
                              onChange={() => toggleSel(String(r.id))}
                            />
                          </td>
                        )}
                        {renderCells(r)}
                        <td className="td w-16 pr-2 whitespace-nowrap">
                          {type === "artist" && (
                            <button
                              className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors"
                              title="Auto-import this artist's discography (one release per release group)"
                              onClick={(e) => {
                                e.stopPropagation();
                                runImport([String(r.id)], "artist", "best");
                              }}
                            >
                              <Zap className="h-4 w-4" />
                            </button>
                          )}
                          <ExtLink href={mbUrl(type, String(r.id))} title="Open on MusicBrainz" />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <LoadMore
                loaded={rows.length}
                total={total}
                busy={search.isFetchingNextPage}
                onLoad={() => search.fetchNextPage()}
              />
            </div>
          </>
        )}
      </div>

      {detail && (
        <DiscoveryAlbumPanel
          row={detail}
          album={album.data}
          loading={album.isLoading}
          error={album.error}
          wished={wished}
          busy={wish.isPending}
          mbid={rgMbid}
          onWish={() => wish.mutate(detail)}
          onViewMb={() => {
            if (rgMbid) nav(`/mb/rg/${rgMbid}`);
          }}
          onClose={() => setDetail(null)}
        />
      )}
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
  const [mode, setMode] = useState<ImportMode>("best");
  const { busy, run } = useAutoImport();
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
  if (isLoading) return <PageLoading label="Asking MusicBrainz…" />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  const a = discography.data?.pages[0];
  if (!a) return null;
  const life = (a.life_span ?? []).filter(Boolean).join(" – ");

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        back={{ to: "/mb/search", label: "MusicBrainz search" }}
        overline="MusicBrainz artist"
        title={a.name}
        subtitle={[a.disambiguation, a.type, a.country, life].filter(Boolean).join(" · ")}
        chips={[...(a.genres ?? []), ...(a.tags ?? []).slice(0, 5)].slice(0, 8)}
        actions={
          <>
            {/* Wishes are per release, so an artist offers only the discography
                import. The server fans an artist out to its release groups by
                itself — best edition each, capped at 50 groups per call — so
                `mode` rides along for the API's shared shape, and "all" still
                means one release per group here. */}
            <Segmented
              value={mode}
              onChange={setMode}
              options={IMPORT_MODES}
              className={busy ? "opacity-60" : ""}
            />
            <button
              className="btn-primary !py-1.5 text-xs"
              disabled={busy}
              title="Find → verify → download → import this artist's release groups from Soulseek (one job at a time)"
              onClick={() => run([String(a.id)], "artist", mode)}
            >
              <Zap className="h-3.5 w-3.5" /> Auto-import
            </button>
            <MbHeaderActions
              href={mbUrl("artist", a.id)}
              query={a.name}
            />
          </>
        }
      />
      <div>
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
                <div className="rounded-lg border border-border overflow-hidden table-scroll">
                  <div className="stagger">
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
  // Bulk auto-import is the whole point of this page, so its editions table
  // carries the library's select-mode: checkbox column, selected rows tinted
  // and a batch bar above the table.
  const [sel, setSel] = useState<string[]>([]);
  const [wished, setWished] = useState(false);
  const [wishBusy, setWishBusy] = useState(false);
  const [mode, setMode] = useState<ImportMode>("best");
  const { busy, run } = useAutoImport();
  const toggleSel = (rid: string) =>
    setSel((s) => (s.includes(rid) ? s.filter((x) => x !== rid) : [...s, rid]));
  // rows the payload could not identify still count as selected, so the batch
  // reports them as skipped instead of queueing `undefined`
  const selIds = sel.filter((x) => x && x !== "undefined");

  if (!id) return null;
  if (isLoading) return <PageLoading label="Asking MusicBrainz…" />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  const rg = editions.data?.pages[0];
  if (!rg) return null;
  const typeLabel = [rg.primary_type, ...(rg.secondary_types ?? [])].filter(Boolean).join(" + ");

  const wishHere = async (mbids: string[]) => {
    setWishBusy(true);
    let added = 0;
    try {
      for (const release_mbid of mbids) {
        try {
          await api.wishAdd({ release_mbid, title: rg.title, artist: rg.artist });
          added += 1;
        } catch {
          /* one refused wish must not abandon the batch */
        }
      }
    } finally {
      setWishBusy(false);
    }
    const missed = mbids.length - added;
    toast.success(`Added ${added} to wishes${missed ? ` · ${missed} skipped` : ""}`);
    return added;
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        back={{ to: "/mb/search", label: "MusicBrainz search" }}
        overline="MusicBrainz release group"
        title={rg.title}
        subtitle={[
          rg.artist,
          typeLabel,
          rg.first_release_date,
          rg.disambiguation,
        ]
          .filter(Boolean)
          .join(" · ")}
        chips={rg.genres ?? []}
        actions={
          <>
            {rg.artist_mbid && (
              <Link className="btn-ghost !py-1.5 text-xs" to={`/mb/artist/${rg.artist_mbid}`}>
                Artist page
              </Link>
            )}
            <Segmented
              value={mode}
              onChange={setMode}
              options={IMPORT_MODES}
              className={busy ? "opacity-60" : ""}
            />
            <button
              className="btn-primary !py-1.5 text-xs"
              disabled={busy}
              title={
                mode === "best"
                  ? "Queue the one edition the auto-import policy prefers for this group"
                  : "Queue every eligible edition of this group (one job at a time)"
              }
              onClick={() => run([String(rg.id)], "release_group", mode)}
            >
              <Zap className="h-3.5 w-3.5" /> Auto-import
            </button>
            <button
              className="btn-ghost !py-1.5 text-xs"
              title="Save this release group to the wishlist — it is auto-imported from Soulseek when a verified copy appears"
              disabled={wishBusy || wished}
              onClick={async () => {
                if (await wishHere([String(rg.id)])) setWished(true);
              }}
            >
              {wished ? <Check className="h-3.5 w-3.5" /> : <BookmarkPlus className="h-3.5 w-3.5" />}
              {wished ? "Wished" : "Add to wishes"}
            </button>
            <MbHeaderActions
              href={mbUrl("release-group", rg.id)}
              query={[rg.artist, rg.title].filter(Boolean).join(" ")}
            />
          </>
        }
      />
      <div>
        <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
          Releases{releasesAll.length < relTotal ? ` · ${releasesAll.length} of ${relTotal}` : ` · ${relTotal}`}
        </div>
        {sel.length > 0 && (
          <div className="panel mb-3 flex items-center gap-2 flex-wrap">
            <span className="text-xs font-medium text-accent-soft">
              {sel.length} edition{sel.length === 1 ? "" : "s"} selected
            </span>
            <div className="ml-auto flex gap-1.5 flex-wrap">
              <button
                className="btn-primary !py-1 text-xs"
                disabled={busy}
                onClick={() => run(selIds, "release_group", "best", sel.length - selIds.length)}
                title="Queue one release per selected group — the edition the auto-import policy prefers"
              >
                <Zap className="h-3.5 w-3.5" /> Auto-import (best per group)
              </button>
              <button
                className="btn-ghost !py-1 text-xs"
                disabled={busy}
                onClick={() => run(selIds, "release_group", "all", sel.length - selIds.length)}
                title="Queue every eligible edition of each selected group"
              >
                Auto-import (all)
              </button>
              <button
                className="btn-ghost !py-1 text-xs"
                disabled={wishBusy}
                onClick={async () => {
                  await wishHere(selIds);
                  setSel([]);
                }}
              >
                <BookmarkPlus className="h-3.5 w-3.5" /> Add to wishes
              </button>
              <button className="btn-ghost !py-1 text-xs" onClick={() => setSel([])}>
                Clear
              </button>
            </div>
          </div>
        )}
        <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${editions.isPlaceholderData ? "opacity-50" : ""}`}>
          <div className="table-scroll">
            <table className="w-full text-sm">
              <thead className="border-b border-border">
                <tr>
                  <th className="th w-8">
                    <input
                      type="checkbox"
                      title="Select every loaded edition"
                      checked={sorted.length > 0 && sel.length === sorted.length}
                      onChange={() =>
                        setSel(
                          sel.length === sorted.length ? [] : sorted.map((r) => String(r.id)).filter(Boolean)
                        )
                      }
                    />
                  </th>
                  <SortTh label="Date" k="date" sort={sort} onSort={onSort} className="w-24 cell-nowrap" />
                  <SortTh label="Title" k="title" sort={sort} onSort={onSort} />
                  <th className="th w-[13%]">Format</th>
                  <SortTh label="Discs" k="disc_count" sort={sort} onSort={onSort} className="w-[8%] cell-nowrap text-right" />
                  <SortTh label="Tracks" k="track_count" sort={sort} onSort={onSort} className="w-[12%] text-right" />
                  <th className="th w-[7%]">Country</th>
                  <th className="th w-[14%]">Barcode</th>
                  <th className="th w-10"></th>
                </tr>
              </thead>
              <tbody className="stagger">
                {sorted.map((r) => (
                  <tr
                    key={r.id}
                    className={`table-row !cursor-pointer ${sel.includes(String(r.id)) ? "bg-accent/15" : ""}`}
                    onClick={() => nav(`/mb/release/${r.id}`)}
                    onMouseEnter={() => prefetch("release", r.id)}
                    title="Open this release"
                  >
                    <td className="td w-8 pr-0" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={sel.includes(String(r.id))}
                        onChange={() => toggleSel(String(r.id))}
                      />
                    </td>
                    <td className="td text-zinc-500 cell-nowrap">{r.date || "—"}</td>
                    <td className="td text-zinc-200">
                      <span className="truncate">{r.title}</span>
                      {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
                    </td>
                    <td className="td text-zinc-500">
                      <span className="inline-flex flex-wrap items-center gap-1">
                        <StatusBadge status={r.status} formats={r.formats} />
                      </span>
                    </td>
                    <td className="td text-zinc-500 text-right">{r.disc_count || "—"}</td>
                    <td
                      className="td text-zinc-500 text-right tabular-nums cell-nowrap"
                      title={(r.disc_count ?? 1) > 1 ? `${r.track_count} tracks across ${r.disc_count} discs` : undefined}
                    >
                      {tracksLabel(r) || "—"}
                    </td>
                    <td className="td text-zinc-500">{r.country || "—"}</td>
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
          </div>
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
  const [mode, setMode] = useState<ImportMode>("best");
  const { busy, run } = useAutoImport();
  if (isLoading) return <PageLoading label="Asking MusicBrainz…" />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  if (!r) return null;

  const cover = `https://coverartarchive.org/release/${r.id}/front-500`;
  const artist = r.artists?.map((a) => a.name).join(", ") || "";
  // The release lookup also answers `status`, `medium` and (sometimes)
  // `artist_mbid`; MBRelease has not caught up with those three yet.
  const extra = r as MBRelease & { status?: string; medium?: string; artist_mbid?: string };
  const artistMbid = r.artists?.[0]?.mbid || extra.artist_mbid || "";
  const meta = [
    typeLabel(r.primary_type, r.secondary_types) || r.release_type,
    r.date,
    [r.label, r.catalog_number].filter(Boolean).join(" · "),
    r.country,
    r.barcode,
  ]
    .filter(Boolean)
    .join(" · ");
  // "best" queues this exact pressing; "all" queues every eligible edition of
  // the release group it belongs to (the backend ignores `mode` for a single
  // release, so the wider scope has to be asked for explicitly).
  const importTarget =
    mode === "all" && r.release_group_id
      ? { mbid: r.release_group_id, kind: "release_group" as const }
      : { mbid: r.id, kind: "release" as const };
  const discs: Record<number, MBTrackRow[]> = {};
  for (const t of (r.media ?? []) as MBTrackRow[]) {
    (discs[t.disc] ??= []).push(t);
  }

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        back={{ to: "/mb/search", label: "MusicBrainz search" }}
        overline="MusicBrainz release"
        title={r.title}
        subtitle={
          <>
            {artistMbid ? (
              <Link
                to={`/mb/artist/${artistMbid}`}
                className="hover:text-accent-soft"
                title="Open the credited artist on MusicBrainz"
              >
                {artist}
              </Link>
            ) : (
              artist
            )}
            {meta ? <> · {meta}</> : null}
            <span className="ml-1.5 inline-flex flex-wrap items-center gap-1 align-middle">
              <StatusBadge status={extra.status} formats={extra.medium} />
            </span>
          </>
        }
        chips={r.genres ?? []}
        actions={
          <>
            <Segmented
              value={mode}
              onChange={setMode}
              options={IMPORT_MODES}
              className={busy ? "opacity-60" : ""}
            />
            <button
              className="btn-primary !py-1.5 text-xs"
              disabled={busy}
              title={
                importTarget.kind === "release_group"
                  ? "Find → verify → download → audit → import every eligible edition of this release group from Soulseek"
                  : "Find → verify → download → audit → import this exact release from Soulseek"
              }
              onClick={async () => {
                await run([importTarget.mbid], importTarget.kind, mode);
                nav(`/soulseek?release=${encodeURIComponent(r.id)}`);
              }}
            >
              <Zap className="h-3.5 w-3.5" /> Auto-import
            </button>
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
                  toast.success("Added to wishes");
                } catch (e) {
                  toast.error(String(e));
                } finally {
                  setWishBusy(false);
                }
              }}
            >
              {wished ? <Check className="h-3.5 w-3.5" /> : <BookmarkPlus className="h-3.5 w-3.5" />}
              {wished ? "Wished" : "Add to wishes"}
            </button>
            {r.release_group_id && (
              <Link className="btn-ghost !py-1.5 text-xs" to={`/mb/rg/${r.release_group_id}`}>
                Release group
              </Link>
            )}
            <a className="btn-ghost !py-1.5 text-xs" href={cover} target="_blank" rel="noreferrer" title="Cover Art Archive">
              Cover art
            </a>
            <MbHeaderActions
              href={mbUrl("release", r.id)}
              query={[r.artists?.[0]?.name, r.title].filter(Boolean).join(" ")}
            />
          </>
        }
      />

      <div className="grid grid-cols-1 lg:grid-cols-[240px_1fr] gap-6">
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
  if (isLoading) return <PageLoading label="Asking MusicBrainz…" />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  const r = appearances.data?.pages[0];
  if (!r) return null;

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        back={{ to: "/mb/search", label: "MusicBrainz search" }}
        overline="MusicBrainz recording"
        title={r.title}
        subtitle={[
          r.artist,
          r.length ? fmtLen(r.length) : "",
          r.disambiguation,
          r.isrcs?.length ? `ISRC ${r.isrcs.join(", ")}` : "",
        ]
          .filter(Boolean)
          .join(" · ")}
        chips={r.genres ?? []}
        actions={
          <>
            {r.artist_mbid && (
              <Link className="btn-ghost !py-1.5 text-xs" to={`/mb/artist/${r.artist_mbid}`}>
                Artist page
              </Link>
            )}
            <MbHeaderActions
              href={mbUrl("recording", r.id)}
              query={[r.artist, r.title].filter(Boolean).join(" ")}
            />
          </>
        }
      />
      <div>
        <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
          Appears on{releasesAll.length < relTotal ? ` · ${releasesAll.length} of ${relTotal} releases` : ` · ${relTotal} releases`}
        </div>
        <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${appearances.isPlaceholderData ? "opacity-50" : ""}`}>
          <div className="table-scroll">
            <table className="w-full text-sm">
              <thead className="border-b border-border">
                <tr>
                  <SortTh label="Date" k="date" sort={sort} onSort={onSort} className="w-24 cell-nowrap" />
                  <SortTh label="Title" k="title" sort={sort} onSort={onSort} />
                  <th className="th w-[13%]">Format</th>
                  <th className="th w-[10%]">Type</th>
                  <SortTh label="Tracks" k="track_count" sort={sort} onSort={onSort} className="w-[11%] text-right" />
                  <th className="th w-[8%]">Country</th>
                  <th className="th w-10"></th>
                </tr>
              </thead>
              <tbody className="stagger">
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
                    <td className="td text-zinc-500">
                      <span className="inline-flex flex-wrap items-center gap-1">
                        <StatusBadge status={rel.status} formats={rel.formats} />
                      </span>
                    </td>
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
          </div>
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
