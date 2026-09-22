import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  keepPreviousData, useInfiniteQuery, useQuery, useQueryClient,
} from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ArrowUpDown, ArrowUpRight, Check, HelpCircle, Library, Loader2, Search, Zap } from "lucide-react";
import { api } from "../api";
import type {
  Library as LibraryData, MBArtistBrowse, MBCountryEvent, MBReleaseGroupBrowse, MBReleaseGroupRow,
  MBReleaseRow, MBRecordingBrowse, MBSearchField, MBSearchRow as MBSearchRowData, MBSearchRows,
} from "../types";
import { albumRef } from "../lib/refs";
import { TABLE_FIT } from "../lib/columns";
import { EmptyState, PageLoading } from "../components/Badges";
import { MbIcon } from "../components/Links";
import PageHeader from "../components/PageHeader";
import Popover from "../components/Popover";
import ReleaseChoice from "../components/ReleaseChoice";
import Segmented from "../components/Segmented";
import { WatchArtistButton } from "../components/WatchDialog";
import { useI18n } from "../lib/i18n";
import { toast } from "../store";

/* In-app MusicBrainz browser: search across the four browsable entities and
 * drill into artist / release-group / release / recording pages. Everything
 * renders as column tables (same language as the library views), pages 100
 * rows at a time, keeps the previous results visible while a new query loads,
 * and prefetches entity pages on row hover. Every paged list finishes ITSELF —
 * see `AutoLoad`, which keeps fetching while there is more — because a list
 * that stopped at its first page read as "MusicBrainz is missing these
 * editions / groups". Bare MusicBrainz IDs and musicbrainz.org links pasted
 * into the search box are detected and routed to their entity page. */

const PAGE = 100;

const TYPES = [
  { id: "artist", label: "Artists" },
  { id: "release-group", label: "Release groups" },
  { id: "release", label: "Releases" },
  { id: "recording", label: "Recordings" },
] as const;
type MBType = (typeof TYPES)[number]["id"];

/** The tab's own label for an entity kind, lower-cased for a sentence
 *  ("release groups") — the help panel's heading and the unknown-field warning
 *  must name the kind exactly as the tab does. */
const kindLabelOf = (kind: MBType) =>
  (TYPES.find((t) => t.id === kind)?.label ?? kind).toLowerCase();

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

interface MBTrackRow {
  disc: number; position: number; title: string; length?: number | null;
  recording_mbid?: string | null; artist_credit?: string;
}
/** One page-row of the search table: the server's row plus the display
 *  fields the table derives from it (a joined life span, a joined tag list,
 *  a formatted length, the combined release type). */
type SearchRow = MBSearchRowData & {
  lifeLabel?: string; tagsLabel?: string; lenLabel?: string; typeLabel?: string;
};
/** "Name (Alias)" — the entity as the reader's locale names it.

 *  MusicBrainz states an entity's other-language names as aliases; the SERVER
 *  picks the one in the configured `beets_locale` (Settings -> Import & tags)
 *  and leaves it out when it would only repeat the name (see
 *  `integrations.alias_for`), so this only adds the parentheses. */
function withAlias(name: string | undefined, alias?: string): string {
  const base = (name || "").trim();
  const alt = (alias || "").trim();
  return alt ? `${base} (${alt})` : base;
}

type RGRow = MBReleaseGroupRow;
type RelRow = MBReleaseRow;
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

/** The library album that already holds one of these MusicBrainz IDs, or null.
 *
 *  Matched on the album's own MB tags — the same evidence the server's
 *  auto-import skip and the wish reconciliation read, so the page never
 *  claims an album the importer would download a second time. The library
 *  payload is the query key the library page already uses, so a visit that
 *  started there costs no extra request. */
function useOwnedAlbum(ids: (string | null | undefined)[]) {
  const want = ids.filter(Boolean).map((x) => String(x).toLowerCase()).join(",");
  const { data: lib } = useQuery<LibraryData>({
    queryKey: ["library"],
    queryFn: api.library,
    staleTime: 30000,
  });
  return useMemo(() => {
    if (!want) return null;
    const wanted = want.split(",");
    for (const artist of lib?.artists ?? []) {
      for (const al of artist.albums ?? []) {
        // A pending framework album carries the release ids but holds no audio
        // yet — "already in your library" would be a lie the download then
        // obeys (the server's own owned check skips them for the same reason).
        if (al.pending) continue;
        const meta = al.meta ?? {};
        const have = [meta.MUSICBRAINZ_ALBUMID, meta.MUSICBRAINZ_RELEASEGROUPID]
          .map((v) => String(v ?? "").toLowerCase());
        if (have.some((h) => h && wanted.includes(h))) return al;
      }
    }
    return null;
  }, [lib, want]);
}

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

/** "best" = the one edition the add/import policy prefers per release group
 *  (default); "all" = every eligible edition. Sent to /api/library/add. */
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

/** "Add to library" with a busy flag: the server creates the FRAMEWORK album
 *  (the folder the naming script names, with the release's own tracklist and
 *  the release-group cover) and starts the search for its audio, so the album
 *  is in the library and visibly pending the moment the button is pressed.
 *  Reports what the server ACTUALLY added (`albums[].created`), never what was
 *  asked for; `missing` is ids that had nothing to send (already counted).
 *  A call that fails toasts the reason instead of a bare "nothing added". */
function useAddToLibrary() {
  const [busy, setBusy] = useState(false);
  const run = async (
    ids: string[],
    kind: "release" | "release_group" | "artist" | "recording",
    mode: ImportMode = "best",
    missing = 0,
    extra: { title?: string; artist?: string; year?: string; release_mbid?: string } = {}
  ) => {
    setBusy(true);
    let added = 0;
    let have = 0;
    let skipped = missing;
    let reason = "";
    try {
      for (const mbid of ids) {
        try {
          const res = await api.libraryAdd({ mbid, kind, mode, ...extra });
          if (res.background) {
            // An artist's discography is prepared off-request; the albums
            // appear (and start searching) as each one is created.
            toast.success(res.note || "Preparing the discography — the albums appear as they are added");
            return;
          }
          added += res.albums.filter((a) => a.created).length;
          have += res.albums.filter((a) => a.already_in_library).length;
          skipped += res.skipped.length + res.errors.length;
        } catch (e) {
          // one bad id must not abandon the rest of the batch
          skipped += 1;
          const msg = e instanceof Error ? e.message : String(e);
          reason ||= /no answer within/i.test(msg)
            ? "MusicBrainz is busy — try again"
            : msg;
        }
      }
    } finally {
      setBusy(false);
    }
    const tail = `${have ? ` · ${have} already in the library` : ""}` +
      `${skipped ? ` · ${skipped} skipped` : ""}${reason ? ` · ${reason}` : ""}`;
    if (added) toast.success(`Added ${added} to your library${tail}`);
    else toast.error(`Nothing added${tail || " — MusicBrainz is busy, try again"}`);
  };
  return { busy, run };
}

/** The library glyph that becomes a spinner while an add is in flight — the
 *  button must look busy, not dead, when MusicBrainz takes a few seconds. */
function AddIcon({ busy }: { busy: boolean }) {
  return busy ? (
    <Loader2 className="h-3.5 w-3.5 animate-spin" />
  ) : (
    <Library className="h-3.5 w-3.5" />
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

/** Footer under a paged list: what is shown, and the REST of the pages.
 *
 *  Every list here is paged, and a list that stopped at the first page until
 *  it was asked for the next one is what made a release group look like it was
 *  missing editions (and an artist's discography look like it was missing
 *  release groups, and their types with it). The tail therefore LOADS ITSELF:
 *  a sentinel at the end of the list is watched with an IntersectionObserver,
 *  and every time it is in view — with more to come, and nothing in flight —
 *  the next page is fetched, until there is nothing left. Each pass re-checks,
 *  so a sentinel that is still on screen after a page lands keeps going without
 *  the user scrolling away and back.
 *
 *  The readout stays honest while it happens: it is the rows actually on
 *  screen, and `hasMore`/`next` are the SERVER's own answer (the payload
 *  carries the offset of the next page, null at the end) rather than a guess
 *  from a row count, which would sit one row below `total` forever when
 *  MusicBrainz repeats a row.
 *
 *  `all` skips the sentinel and fetches every page whether or not the tail is
 *  on screen: the artist page's type chips and its by-release-type panel are
 *  both derived from the LOADED groups, so that one discography has to be
 *  complete before either can claim to name every type the artist has.
 *
 *  `next` is what proves a pass made progress. A pass that leaves it where it
 *  was — an error, or a page MusicBrainz answered without advancing — ends the
 *  automatic loading and leaves the button, because retrying the same offset
 *  would spin there forever. The button is also what a browser without
 *  IntersectionObserver gets; the two are never both rendered. */
function AutoLoad({ loaded, total, next, busy, hasMore, all, onLoad }: {
  loaded: number; total: number; next?: number | null; busy: boolean;
  hasMore: boolean;
  /** fetch every page, not only while the tail is on screen */
  all?: boolean;
  onLoad: () => void;
}) {
  const sentinel = useRef<HTMLDivElement | null>(null);
  // The callback is a fresh closure every render; the pass reads it through a
  // ref so its identity never re-triggers an effect.
  const load = useRef(onLoad);
  load.current = onLoad;
  const [seen, setSeen] = useState(false);
  const [stalled, setStalled] = useState(false);
  // What the pass now in flight started from ("" while none is in flight), and
  // whether that pass has actually been seen on the wire (`busy`). The second
  // flag is what makes a duplicate effect run — StrictMode's double mount, or
  // any render between "asked for" and "fetching" — harmless: without it, a
  // run that happened before the fetch was observable looked like a pass that
  // had ended without bringing anything, and the list stopped one page in.
  const from = useRef("");
  const onWire = useRef(false);
  // One automatic second try: a pass that brought nothing stops the automatic
  // loading, and a page that failed while MusicBrainz was busy is exactly the
  // case nobody should have to click for. After a moment the sentinel tries
  // once more; a second empty pass leaves the button for good.
  const [retried, setRetried] = useState(false);
  const canObserve = typeof IntersectionObserver !== "undefined";
  const here = `${loaded}:${next}`;

  useEffect(() => {
    const el = sentinel.current;
    if (!canObserve || !el || !hasMore) return;
    const io = new IntersectionObserver(
      (entries) => setSeen(entries.some((e) => e.isIntersecting)),
      { rootMargin: "400px" },  // start before the tail is actually on screen
    );
    io.observe(el);
    return () => io.disconnect();
  }, [canObserve, hasMore]);

  useEffect(() => {
    if (!stalled || retried || !hasMore) return;
    const t = setTimeout(() => { setRetried(true); setStalled(false); }, 4000);
    return () => clearTimeout(t);
  }, [stalled, retried, hasMore]);

  // A page landing re-runs this, which is the whole auto-load: the sentinel
  // may still be in view, so the next page follows on its own.
  useEffect(() => {
    if (busy) {
      if (from.current) onWire.current = true;
      return;
    }
    if (from.current) {
      const moved = from.current !== here;
      // Asked for, and nothing has happened yet: not an outcome at all.
      if (!onWire.current && !moved) return;
      onWire.current = false;
      from.current = "";
      if (!moved) {
        setStalled(true);
        return;
      }
      setStalled(false);
    }
    if (!hasMore || stalled || !canObserve || (!all && !seen)) return;
    from.current = here;
    onWire.current = false;
    load.current();
  }, [canObserve, all, seen, busy, hasMore, stalled, here]);

  if (!hasMore) return null;
  const label = `showing ${loaded} of ${total}`;
  if (!canObserve || stalled) {
    return (
      <button
        className="w-full py-2 text-xs text-zinc-400 hover:text-white hover:bg-raise transition-colors disabled:opacity-50 flex items-center justify-center gap-1.5"
        onClick={() => { setStalled(false); onLoad(); }}
        disabled={busy}
      >
        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
        Load more — {label}
      </button>
    );
  }
  return (
    <div
      ref={sentinel}
      className="py-2 text-xs text-zinc-500 flex items-center justify-center gap-1.5"
    >
      {busy ? (
        <>
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Loading more — {label}
        </>
      ) : null}
    </div>
  );
}

/** One index-constraint box (artist / year / label / cat #).
 *
 *  Local text + the same 400 ms debounce the search box uses: typing
 *  "Radiohead" into the URL on every keystroke would be nine searches behind
 *  MusicBrainz's 1 req/s etiquette. Enter applies at once. */
function ConstraintBox({ value, placeholder, title, onSet }: {
  value: string; placeholder: string; title: string; onSet: (v: string) => void;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]); // stay in sync with back/forward
  useEffect(() => {
    if (text === value) return;
    const t = setTimeout(() => onSet(text), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  return (
    <input
      className="input !py-1 !w-40 text-xs"
      placeholder={placeholder}
      title={title}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") onSet(text);
      }}
    />
  );
}

/* ---- client-side column sorting ------------------------------------------ */

interface SortState { key: string; dir: 1 | -1 }

function useSort<T extends object>(rows: T[], defaultKey: string | null) {
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
      // the key is a column id, so it is only known at runtime
      const v = (r as Record<string, unknown>)[sort.key];
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

/** Warm an entity page while the pointer RESTS on a row that links to it —
 * by click time the payload is usually already in the query cache.
 *
 *  MusicBrainz answers one request per second, so a warming request per row
 *  is not free: sweeping the pointer across a hundred rows used to queue a
 *  hundred entity fetches behind that etiquette. The pointer has to dwell on
 *  a row before anything is asked, an already-warm (or in-flight) payload is
 *  never asked for twice, and `cool` clears a pending warm when the pointer
 *  leaves. */
function useMbPrefetch() {
  const qc = useQueryClient();
  const timer = useRef<number | null>(null);

  const clear = () => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  };
  useEffect(() => clear, []);

  const warm = (type: string, id: string) => {
    if (!id) return;
    clear();
    const key =
      type === "release" ? ["mbRelease", id] : [`mb${type === "release-group" ? "RG" : type === "artist" ? "Artist" : "Recording"}`, id];
    if (qc.getQueryData(key) !== undefined) return; // already in the cache
    timer.current = setTimeout(() => {
      timer.current = null;
      if (qc.getQueryData(key) !== undefined) return;
      if (type === "artist") {
        qc.prefetchInfiniteQuery({
          queryKey: key,
          queryFn: ({ pageParam }) => api.mbArtist(id, pageParam),
          initialPageParam: 0,
        });
      } else if (type === "release-group") {
        qc.prefetchInfiniteQuery({
          queryKey: key,
          queryFn: ({ pageParam }) => api.mbReleaseGroup(id, pageParam),
          initialPageParam: 0,
        });
      } else if (type === "release") {
        qc.prefetchQuery({ queryKey: key, queryFn: () => api.mbRelease(id) });
      } else if (type === "recording") {
        qc.prefetchInfiniteQuery({
          queryKey: key,
          queryFn: ({ pageParam }) => api.mbRecording(id, pageParam),
          initialPageParam: 0,
        });
      }
    }, 150);
  };
  return { warm, cool: clear };
}

/* ------------------------------------------------------------------ */
/* The search box's own help                                           */
/* ------------------------------------------------------------------ */

/** MusicBrainz's search-field catalogue, cached for the session: it only
 *  changes when the server's own catalogue does (the same idea as the library
 *  query builder's `useLibraryFields`). */
function useMbSearchFields() {
  return useQuery({
    queryKey: ["mbSearchFields"],
    queryFn: api.mbSearchFields,
    staleTime: 30 * 60_000,
  });
}

/** Every field of the entity kind being searched, plus the syntax they combine
 *  with, and a one-click insert: the snippet goes into the search box and the
 *  caret is left where the value goes (`artist:""` → between the quotes),
 *  because the box's query IS the Lucene query.
 *
 *  Nothing here hardcodes a field: the list is the server's own catalogue, so
 *  the help can never teach a field the index would not answer. */
function SearchFieldHelp({ kind, onInsert }: {
  kind: MBType;
  onInsert: (snippet: string, caretAt: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const { data, isError } = useMbSearchFields();
  const syntax = data?.syntax ?? [];
  const fields = data?.fields?.[kind] ?? [];
  // Filtered here rather than server-side: the catalogue is a few dozen rows
  // that never change while the page is open.
  const needle = q.trim().toLowerCase();
  const shown = needle
    ? fields.filter((f) => f.field.includes(needle) || f.meaning.toLowerCase().includes(needle))
    : fields;
  const kindLabel = kindLabelOf(kind);
  const pick = (snippet: string, caretAt: number) => {
    setOpen(false);
    setQ("");
    onInsert(snippet, caretAt);
  };
  /** `artist:""` for a quoted value (the caret lands between the quotes),
   *  `country:` for a code/date/id one, which is never quoted. */
  const insert = (f: MBSearchField) =>
    pick(`${f.field}:${f.quotes ? '""' : ""}`, f.field.length + (f.quotes ? 2 : 1));

  return (
    <div className="relative shrink-0">
      <button
        className="btn-ghost !py-1 text-[11px] flex items-center gap-1"
        onClick={() => setOpen(!open)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={`MusicBrainz search syntax, and every field its index answers for this search — click one to insert it into the box`}
      >
        <HelpCircle className="h-3.5 w-3.5" /> Fields &amp; syntax
      </button>
      <Popover open={open} onClose={() => setOpen(false)} align="left"
               panelClass="w-[min(30rem,calc(100vw-1rem))] max-h-[70vh] overflow-y-auto p-1.5">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2.5 pt-1.5 pb-1">Syntax</div>
        {syntax.map((s) => (
          <button
            key={s.form}
            role="menuitem"
            onClick={() => pick(s.form, s.form.length)}
            title={s.meaning}
            className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-white/10 transition-colors tap"
          >
            <div className="font-mono text-[11px] text-zinc-200 truncate">{s.form}</div>
            <div className="text-[11px] text-zinc-500">{s.meaning}</div>
          </button>
        ))}
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2.5 pt-3 pb-1">
          Fields · {kindLabel}
        </div>
        <div className="sticky top-0 -mt-1.5 pt-1.5 pb-1 bg-zinc-950 z-10">
          <div className="relative">
            <Search className="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-600" />
            <input
              className="input !py-1 text-xs pl-8"
              placeholder="Find a field…"
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        </div>
        {!data && !isError && (
          <div className="text-xs text-zinc-500 px-2.5 py-2">Loading the field catalogue…</div>
        )}
        {isError && (
          <div className="text-xs text-amber-300/80 px-2.5 py-2">
            The field catalogue could not be loaded. The box still takes any MusicBrainz field
            you type — the syntax above is the whole of it.
          </div>
        )}
        {data && !shown.length && (
          <div className="text-xs text-zinc-500 px-2.5 py-2">No field of this search matches.</div>
        )}
        {shown.map((f) => (
          <button
            key={f.field}
            role="menuitem"
            onClick={() => insert(f)}
            title={`${f.meaning}${f.quotes ? " — quoted, so a multi-word value stays one phrase" : " — never quoted"} — e.g. ${f.field}:${f.example}`}
            className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-white/10 transition-colors tap"
          >
            <div className="flex items-baseline gap-2 text-[11px] min-w-0">
              <span className="font-mono text-zinc-200 shrink-0">{f.field}:</span>
              <span className="font-mono text-zinc-500 truncate">{f.example}</span>
            </div>
            <div className="text-[11px] text-zinc-500">{f.meaning}</div>
          </button>
        ))}
      </Popover>
    </div>
  );
}

/** Field names a query uses that MusicBrainz's index does not have for this
 *  entity kind.
 *
 *  MusicBrainz does NOT reject an unknown field — its index falls back to a
 *  full-text search of the value (verified live: `bogusfield:"paranoid
 *  android"` answers 20 releases, not an error), so a typo would otherwise
 *  answer with unrelated rows and say nothing. The catalogue is the only thing
 *  that can tell the user which field was wrong.
 *
 *  A field is a bare word followed by `:` OUTSIDE quotes (the value of
 *  `artist:"a:b"` is a phrase, not a field); a leading `-`/`+` is the Lucene
 *  exclusion/inclusion prefix, and `\x` is an escaped literal. */
function unknownSearchFields(query: string, fields?: MBSearchField[]): string[] {
  if (!fields?.length) return [];      // catalogue not loaded: claim nothing
  const known = new Set(fields.map((f) => f.field));
  const out: string[] = [];
  let quoted = false;
  for (let i = 0; i < query.length; i++) {
    const ch = query[i];
    if (ch === "\\") {
      i++;                             // the escaped character is a literal
      continue;
    }
    if (ch === '"') {
      quoted = !quoted;
      continue;
    }
    if (quoted || !/[A-Za-z_]/.test(ch)) continue;
    let end = i;
    while (end < query.length && /[A-Za-z_0-9]/.test(query[end])) end++;
    const word = query.slice(i, end);
    const start = i;
    i = end - 1;
    if (query[end] !== ":") continue;                       // not a field
    if (start > 0 && !/[\s(+-]/.test(query[start - 1])) continue;  // a value
    if (!known.has(word) && !out.includes(word)) out.push(word);
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* Search                                                              */
/* ------------------------------------------------------------------ */

/** Column layout per entity — same table language as the library views. */
const COLUMNS: Record<MBType, { k: string; label: string; className?: string }[]> = {
  // Every column carries a PX floor: a table is `table-layout: fixed`
  // (index.css), so TABLE_FIT's `min-w-max` is the sum of the columns — with
  // percentages or auto widths the table has no floor to stop at and the
  // fixed layout squeezes the title one character per line (measured: 12 px at
  // 820). The floors are the widths one row's own text needs; two long ones
  // stack (a row grows), which is what the old proportional widths relied on.
  artist: [
    { k: "title", label: "Name", className: "w-[240px]" },
    { k: "type", label: "Type", className: "w-[96px]" },
    { k: "country", label: "Country", className: "w-[130px]" },
    { k: "lifeLabel", label: "Life span", className: "w-[140px]" },
    { k: "tagsLabel", label: "Tags", className: "w-[180px]" },
    { k: "score", label: "Score", className: "w-[64px] cell-nowrap text-right" },
  ],
  "release-group": [
    { k: "title", label: "Title", className: "w-[220px]" },
    { k: "artist", label: "Artist", className: "w-[200px]" },
    { k: "typeLabel", label: "Type", className: "w-[120px]" },
    { k: "first_release_date", label: "First released", className: "w-[110px]" },
    { k: "score", label: "Score", className: "w-[64px] cell-nowrap text-right" },
  ],
  release: [
    { k: "title", label: "Title", className: "w-[220px]" },
    { k: "artist", label: "Artist", className: "w-[160px]" },
    { k: "typeLabel", label: "Type", className: "w-[96px]" },
    { k: "date", label: "Date", className: "w-[96px]" },
    { k: "formats", label: "Format", className: "w-[110px]" },
    { k: "track_count", label: "Tracks", className: "w-[64px] text-right" },
    { k: "country", label: "Country", className: "w-[72px]" },
    { k: "catalog_number", label: "Cat #", className: "w-[150px]" },
    { k: "score", label: "Score", className: "w-[56px] cell-nowrap text-right" },
  ],
  recording: [
    { k: "title", label: "Title", className: "w-[220px]" },
    { k: "artist", label: "Artist", className: "w-[200px]" },
    { k: "lenLabel", label: "Length", className: "w-[72px]" },
    { k: "first_release_date", label: "First released", className: "w-[110px]" },
    { k: "score", label: "Score", className: "w-[64px] cell-nowrap text-right" },
  ],
};


export function MBSearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const type = (params.get("type") as MBType) || "release";
  const ptype = params.get("ptype") ?? "";
  const stype = params.get("stype") ?? "";
  // Index constraints: MusicBrainz's own fields, each one ANDed into the
  // Lucene query server-side. Filtering the returned rows instead cannot
  // answer them — the rows that would match are not in the page.
  const artist = params.get("artist") ?? "";
  const year = params.get("year") ?? "";
  const label = params.get("label") ?? "";
  const catno = params.get("catno") ?? "";
  const hasConstraints = !!(artist.trim() || year.trim() || label.trim() || catno.trim());
  const [text, setText] = useState(q);
  const nav = useNavigate();
  const { warm: prefetch, cool: unprefetch } = useMbPrefetch();
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => setText(q), [q]); // stay in sync with back/forward

  const pushParams = (nextQ: string) => {
    // Rebuilt from the CURRENT params so the type filter and the constraints
    // survive a new query.
    const next = new URLSearchParams(params);
    if (nextQ.trim()) next.set("q", nextQ.trim());
    else next.delete("q");
    next.set("type", type);
    setParams(next, { replace: true });
  };

  /** Put a snippet into the box at the caret and leave the caret where its
   *  value goes (`artist:""` → between the quotes), then search at once: a
   *  click is an explicit action, so it skips the typing debounce exactly like
   *  Enter does.
   *
   *  Clauses that are merely space-separated are ORed by MusicBrainz's index
   *  (verified live: `artist:"Radiohead" tag:"art rock"` answers 10,624 release
   *  groups against 26 with AND), so a snippet appended to an existing clause
   *  gets an explicit ` AND ` instead of a space — otherwise a one-click insert
   *  would silently WIDEN the search. Inside an open quote, after a bracket, or
   *  on empty/whitespace-before text, nothing is added. */
  const insertIntoQuery = (snippet: string, caretAt: number) => {
    const el = inputRef.current;
    const from = el?.selectionStart ?? text.length;
    const to = el?.selectionEnd ?? from;
    const before = text.slice(0, from);
    const inQuote = ((before.match(/(^|[^\\])"/g) || []).length % 2) === 1;
    const glue = before && !/\s$/.test(before) && !inQuote && !/[([{]$/.test(before) ? " AND " : "";
    const next = text.slice(0, from) + glue + snippet + text.slice(to);
    const caret = from + glue.length + caretAt;
    setText(next);
    pushParams(next);
    // After the re-render that carries the new value: the caret cannot be put
    // inside text the input does not hold yet.
    requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.setSelectionRange(caret, caret);
    });
  };

  const setTypeFilter = (key: "ptype" | "stype", value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const setConstraint = (key: "artist" | "year" | "label" | "catno", value: string) => {
    const next = new URLSearchParams(params);
    if (value.trim()) next.set(key, value.trim());
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const clearConstraints = () => {
    const next = new URLSearchParams(params);
    for (const key of ["artist", "year", "label", "catno"]) next.delete(key);
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
  const urlKind = urlMatch?.[1] ?? null;
  const urlId = urlMatch?.[2] ?? null;
  const bareId = !urlMatch && MBID_RE.test(q.trim()) ? q.trim() : null;
  const detect = useQuery({
    queryKey: ["mbIdentify", bareId],
    queryFn: () => api.mbIdentify(bareId!),
    enabled: !!bareId,
    retry: false,
  });
  useEffect(() => {
    if (urlKind && urlId) nav(`/mb/${routeFor(urlKind)}/${urlId}`);
  }, [urlKind, urlId, nav]);
  const identified = detect.data;
  useEffect(() => {
    if (identified) nav(`/mb/${routeFor(identified.type)}/${identified.id}`);
  }, [identified, nav]);

  const idLike = !!urlMatch || !!bareId;

  // Catalog numbers and barcodes ("SRCS 8757") often don't rank in a free
  // text search — when the query looks like one, run the exact catno/barcode
  // search instead and fall back to free text only if it comes up empty.
  const looksCatno = /^[a-z0-9]{1,8}[\s-]?\d{3,8}([-]?\d{1,4})?$/i.test(q.trim());
  const looksBarcode = /^\d{8,14}$/.test(q.trim());
  const mode = type === "release" ? (looksBarcode ? "barcode" : looksCatno ? "catno" : "free") : "free";
  // The fallback is decided ONCE, from the first page: asking per page mixed
  // pages of two different queries (exact for page 1, free text for page 2)
  // into one result list.
  const [freeTextFallback, setFreeTextFallback] = useState(false);
  const searchMode = freeTextFallback ? "free" : mode;

  const search = useInfiniteQuery({
    queryKey: ["mbSearch", type, q, searchMode, ptype, stype, artist, year, label, catno],
    queryFn: ({ pageParam }) =>
      api.mbSearch({
        type, q, limit: PAGE, mode: searchMode, offset: pageParam as number,
        primaryType: ptype, secondaryType: stype,
        artist, year, label, catno,
      }),
    initialPageParam: 0,
    // The server hands back the offset of the next page (null at the end), so
    // paging never re-derives an offset from de-duplicated row counts.
    getNextPageParam: (last: MBSearchRows) => last.next ?? undefined,
    enabled: (q.trim().length >= 2 || hasConstraints) && !idLike,
    placeholderData: keepPreviousData, // keep rows visible while re-querying
  });

  useEffect(() => { setFreeTextFallback(false); }, [q, type, mode]);
  useEffect(() => {
    const pages = search.data?.pages ?? [];
    if (!freeTextFallback && mode !== "free" && pages.length === 1 && !pages[0].rows.length
        && (pages[0].total ?? 0) === 0) {
      setFreeTextFallback(true); // the exact search has nothing: show free text
    }
  }, [search.data, mode, freeTextFallback]);

  // Rows are de-duplicated by id: MusicBrainz repeats an entity when a query
  // matches it twice (a multi-disc release), and the same row twice in a table
  // is a bug, not a second result.
  const rows = useMemo(() => {
    const seen = new Set<string>();
    const out: MBSearchRowData[] = [];
    for (const page of search.data?.pages ?? []) {
      for (const row of page.rows ?? []) {
        const id = String(row.id ?? "");
        if (!id || seen.has(id)) continue;
        seen.add(id);
        out.push(row);
      }
    }
    return out;
  }, [search.data]);
  const total = search.data?.pages.at(-1)?.total ?? 0;
  // The Lucene query MusicBrainz actually answered — shown above the table so
  // a surprising result list can be read (and re-run on musicbrainz.org).
  const shownQuery = search.data?.pages[0]?.query ?? "";
  // A field MusicBrainz's index does not have is NOT an error there — it
  // searches the value as plain text — so the only honest warning is which
  // field was not its own.
  const catalogue = useMbSearchFields();
  const unknownFields = unknownSearchFields(q, catalogue.data?.fields?.[type]);
  const kindLabel = kindLabelOf(type);
  const unknownNote = unknownFields.length
    ? ` MusicBrainz has no ${unknownFields.map((f) => `${f}:`).join(", ")} field for`
      + ` ${kindLabel} — its index searched the value as plain text.`
    : "";

  // Flatten entity-specific shapes into sortable flat rows for the columns.
  const shaped = useMemo(
    () =>
      rows.map((r): SearchRow => {
        if (type === "artist")
          return {
            ...r,
            lifeLabel: (r.life_span ?? []).filter(Boolean).join(" – "),
            tagsLabel: (r.tags ?? []).join(" · "),
          };
        if (type === "release-group")
          return { ...r, typeLabel: typeLabel(r.primary_type, r.secondary_types) };
        if (type === "recording") return { ...r, lenLabel: r.length ? fmtLen(Number(r.length)) : "" };
        return { ...r, typeLabel: typeLabel(r.primary_type, r.secondary_types) };
      }),
    [rows, type]
  );
  const { sort, onSort, sorted } = useSort(shaped, null);

  // Bulk "Add to library" works off release-group ids, so selection only
  // exists on that tab; it resets whenever the tab or the query changes.
  const [sel, setSel] = useState<string[]>([]);
  const { busy: importBusy, run: runImport } = useAddToLibrary();
  useEffect(() => setSel([]), [type, q]);
  const selectable = type === "release-group";
  const toggleSel = (id: string) =>
    setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  // a row the payload could not identify still counts as selected, so the
  // batch reports it as skipped instead of queueing `undefined`
  const selIds = sel.filter((x) => x && x !== "undefined");

  /** The credited artist, linked to its MusicBrainz page when the row carries
   *  an id — search rows usually do not, so plain text is the common case. */
  const artistCell = (r: SearchRow) => {
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

  const renderCells = (r: SearchRow) => {
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
            <td className="td text-zinc-500">{r.lifeLabel || "—"}</td>
            <td className="td text-zinc-500 truncate">{r.tagsLabel || "—"}</td>
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
            <td className="td text-zinc-500 font-mono">{r.lenLabel || "—"}</td>
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
            ref={inputRef}
            className="input !pl-10"
            placeholder={'Search artists, releases, recordings… — or paste an MB ID / link. Field queries work too: artist:"Radiohead"'}
            title={'MusicBrainz answers the box as a query, not a phrase: field:"value" narrows by one field, AND/OR/NOT combine them (the “Fields & syntax” button lists every field).'}
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
          {/* The box's own help: the entity kind being searched decides which
              fields are offered, so a release tab can never suggest `tnum`. */}
          <SearchFieldHelp kind={type} onInsert={insertIntoQuery} />
        </div>

        {/* Release types are two axes in MusicBrainz: a primary type (Album /
            Single / EP / …) and any number of secondary types (Soundtrack /
            Live / Compilation / …) — both narrow the search server-side. */}
        {(type === "release" || type === "release-group") && (
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

        {/* The constraints the index can answer and a row filter cannot:
            artist:"…" date:[…] label:"…" catno:"…" are ANDed into one WS/2
            query, so "albums by X from 1999 on label Y" is asked, not
            approximated. */}
        {(type === "release" || type === "release-group") && (
          <div className="flex items-center gap-2 flex-wrap">
            <ConstraintBox
              value={artist}
              placeholder="Artist"
              title="Only releases credited to this artist (artist:)"
              onSet={(v) => setConstraint("artist", v)}
            />
            <ConstraintBox
              value={year}
              placeholder="Year"
              title="A year, or a range like 1990-1999 — releases date the pressing, release groups their earliest release"
              onSet={(v) => setConstraint("year", v)}
            />
            {type === "release" && (
              <>
                <ConstraintBox
                  value={label}
                  placeholder="Label"
                  title="Only releases on this label (label:)"
                  onSet={(v) => setConstraint("label", v)}
                />
                <ConstraintBox
                  value={catno}
                  placeholder="Cat #"
                  title="Only releases with this catalog number (catno:)"
                  onSet={(v) => setConstraint("catno", v)}
                />
              </>
            )}
            {hasConstraints && (
              <button className="btn-ghost !py-1 text-[11px]" onClick={clearConstraints}>
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
        ) : !(q.trim().length >= 2 || hasConstraints) ? (
          <EmptyState
            title="Type at least two characters, or set a constraint"
            hint={'Results come straight from musicbrainz.org (rate-limited to 1 request/second — repeated searches are cached and pages prefetch when the pointer rests on a row). The box is a MusicBrainz query, so artist:"Radiohead" AND releasegroup:"OK Computer" narrows it by field — “Fields & syntax” lists every field the index answers.'}
          />
        ) : search.isLoading || (search.isPlaceholderData && !rows.length) ? (
          <PageLoading label="Asking MusicBrainz…" />
        ) : search.error ? (
          <LoadError e={search.error} />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No results"
            hint={(shownQuery ? `MusicBrainz matched nothing for ${shownQuery}.` : "Nothing on MusicBrainz for this search.") + unknownNote}
          />
        ) : (
          <>
            {unknownFields.length > 0 && (
              <div className="mb-3 rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-[11px] text-amber-200/90">
                MusicBrainz's index has no {unknownFields.map((f) => `${f}:`).join(", ")} field
                for {kindLabel} — it searched that value as plain text, so these rows may be
                unrelated. “Fields &amp; syntax” above lists the fields it does have.
              </div>
            )}
            <div className="mb-3 flex items-center justify-between gap-3 text-[11px] text-zinc-500 flex-wrap">
              <span>
                {rows.length} of {total} result{total === 1 ? "" : "s"} loaded
                {freeTextFallback ? " · free-text fallback (the exact catalog-number search had nothing)" : ""}
              </span>
              {shownQuery ? (
                <a
                  className="font-mono truncate max-w-[60%] hover:text-accent-soft"
                  href={`https://musicbrainz.org/search?query=${encodeURIComponent(shownQuery)}` +
                        `&type=${type === "release-group" ? "release_group" : type}&method=indexed`}
                  target="_blank"
                  rel="noreferrer"
                  title={`The query this list came from — MusicBrainz answers it in score order:\n${shownQuery}`}
                >
                  {shownQuery}
                </a>
              ) : null}
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
                    title="Add the edition the policy prefers for each selected group to your library and start searching for it"
                  >
                    <AddIcon busy={importBusy} /> {importBusy ? "Adding…" : "Add to library"}
                  </button>
                  <button className="btn-ghost !py-1 text-xs" onClick={() => setSel([])}>
                    Clear
                  </button>
                </div>
              </div>
            )}
            <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${search.isPlaceholderData ? "opacity-50" : ""}`}>
              <div className="table-scroll">
                <table className={`${TABLE_FIT} text-sm`}>
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
                                  : sorted.map((r) => String(r.id)).filter(Boolean)
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
                    {sorted.map((r) => (
                      <tr
                        key={String(r.id)}
                        className={`table-row !cursor-pointer ${sel.includes(String(r.id)) ? "bg-accent/15" : ""}`}
                        onClick={() => nav(`/mb/${routeFor(type)}/${r.id}`)}
                        onMouseEnter={() => prefetch(type, String(r.id))}
                        onMouseLeave={unprefetch}
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
                              title="Add this artist's release groups to your library and start searching for them"
                              onClick={(e) => {
                                e.stopPropagation();
                                runImport([String(r.id)], "artist", "best");
                              }}
                            >
                              <Library className="h-4 w-4" />
                            </button>
                          )}
                          {type === "recording" && (
                            <button
                              className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors"
                              title="Add the release this recording appears on to your library and start searching for it"
                              onClick={(e) => {
                                e.stopPropagation();
                                runImport([String(r.id)], "recording", "best");
                              }}
                            >
                              <Library className="h-4 w-4" />
                            </button>
                          )}
                          <ExtLink href={mbUrl(type, String(r.id))} title="Open on MusicBrainz" />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {/* Keyed by the query: a new search is a new list, so the
                  auto-load's own state (and a stall a previous search hit)
                  starts over with it rather than leaking into the next one. */}
              <AutoLoad
                key={[type, q, searchMode, ptype, stype, artist, year, label, catno].join("|")}
                loaded={rows.length}
                total={total}
                next={search.data?.pages.at(-1)?.next}
                busy={search.isFetchingNextPage}
                hasMore={search.hasNextPage}
                onLoad={() => search.fetchNextPage()}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Artist                                                              */
/* ------------------------------------------------------------------ */

/** The artist's release groups grouped by TYPE, in the order MusicBrainz served
 *  them ("Album + Compilation" — primary type first, then every secondary
 *  type) — the page's sections and its action rows are one and the same
 *  categorisation, so a row can never offer a type the sections do not show. */
function byReleaseGroupType(groups: RGRow[]): { label: string; list: RGRow[] }[] {
  const order: string[] = [];
  const byLabel = new Map<string, RGRow[]>();
  for (const rg of groups) {
    const label = [rg.primary_type || "Other", ...(rg.secondary_types ?? [])].join(" + ");
    const list = byLabel.get(label);
    if (list) list.push(rg);
    else {
      byLabel.set(label, [rg]);
      order.push(label);
    }
  }
  return order.map((label) => ({ label, list: byLabel.get(label) as RGRow[] }));
}

/** One action row: the type it acts on (as MusicBrainz spells it, a compound
 *  "Album + Live" included), the type SELECTION the server is asked for, and
 *  how many of the artist's release groups it covers. `count` is null while
 *  the discography is still loading. */
interface TypeActionRow { label: string; types: string[]; count: number | null }

/** The artist's whole action block: one Add to library button for the
 *  discography and one for EVERY release-group type it actually has.
 *
 *  `groups` is the page's own discography — the very list the type chips above
 *  filter — so a row and a chip are one thing seen twice: a type the panel can
 *  add is a type the chips can show, and the counts agree. Each row sends its
 *  type as ONE selection ("Album + Live"), which the server matches against a
 *  group's WHOLE type (primary + exactly those secondaries) through
 *  `mlo.release_choice.type_matches`, and the server starts the search for
 *  each album as it records it, so one button is the whole action: the album
 *  is in the library and Soulseek is already looking for its audio. (There
 *  used to be a second "Download all" button; it differed only in a flag that
 *  asked for exactly this, so it said the same thing twice.) Each row owns its
 *  own busy state (a running add disables that row, never the page), and the
 *  server's own answer — what it queued, what it skipped and why, or the
 *  switch that stopped it — lands directly under the row that asked for it.
 *  The queue's own view is refetched on success, so the albums these buttons
 *  created show up there. */
function ArtistTypeActions({ artistId, mode, groups, total, loading }: {
  artistId: string; mode: ImportMode; groups: RGRow[]; total: number; loading: boolean;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState<Record<string, { text: string; ok: boolean }>>({});
  // The whole-artist row first, then one per type the artist actually has,
  // most-populated first (the type a user is most likely after on top).
  const rows: TypeActionRow[] = useMemo(() => {
    const whole: TypeActionRow = {
      label: t("mb.actions_whole"), types: [], count: loading ? null : total,
    };
    const perType = byReleaseGroupType(groups).map(({ label, list }) => ({
      label, types: [label.toLowerCase()], count: list.length,
    }));
    return [whole, ...perType.sort((a, b) => b.count - a.count
                                    || a.label.localeCompare(b.label))];
  }, [groups, total, loading, t]);

  const run = async (row: TypeActionRow) => {
    setBusy(row.label);
    try {
      const res = await api.libraryAdd({
        mbid: artistId, kind: "artist", mode, types: row.types,
      });
      // The server's own words: the counts it answered with, its note (which
      // is the AUTO_OFF sentence when the switch is off), and WHY it skipped —
      // one reason per kind of skip, so "87 skipped" of four different types
      // does not read as one reason repeated.
      const counts = [
        typeof res.queued === "number" ? t("mb.actions_queued", { n: res.queued }) : "",
        res.skipped?.length ? t("mb.actions_skipped", { n: res.skipped.length }) : "",
        res.errors?.length ? t("mb.actions_failed", { n: res.errors.length }) : "",
      ].filter(Boolean).join(" · ");
      const why = [...new Set([...(res.errors ?? []), ...(res.skipped ?? [])]
        .map((row) => row.reason)
        .filter((r): r is string => !!r))]
        .slice(0, 3)
        .join(" · ");
      setSaid((prev) => ({
        ...prev,
        [row.label]: { ok: res.ok,
                       text: [counts, res.note, why].filter(Boolean).join(" — ")
                             || t("mb.actions_nothing") },
      }));
      // Those albums exist now, so the queue (and the library that lists a
      // pending album) is stale — that is the "refetch after a successful
      // action" the rows are judged by.
      qc.invalidateQueries({ queryKey: ["wishes"] });
      qc.invalidateQueries({ queryKey: ["library"] });
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e);
      setSaid((prev) => ({ ...prev, [row.label]: { text, ok: false } }));
      toast.error(text);
    } finally {
      setBusy("");
    }
  };

  const spinner = <Loader2 className="h-3.5 w-3.5 animate-spin" />;
  return (
    <div className="rounded-lg border border-border mb-4">
      <div className="px-3 py-2 border-b border-border text-[11px] uppercase tracking-widest text-zinc-500">
        {t("mb.actions_title")}
      </div>
      <div className="p-1.5">
        {rows.map((row) => {
          const mine = busy === row.label;
          const answer = said[row.label];
          return (
            <div key={row.label} className="rounded-md px-1.5 py-1 hover:bg-raise/60">
              <div className="flex items-center gap-2">
                <span className="text-sm text-zinc-300 truncate flex-1 min-w-0">
                  {row.label}
                  {row.count !== null ? (
                    <span className="text-zinc-500 text-xs">
                      {" · "}{t("mb.actions_groups", { n: row.count })}
                    </span>
                  ) : null}
                </span>
                <button
                  className="btn-ghost !py-1 text-xs shrink-0"
                  disabled={mine}
                  title={t("mb.actions_add_hint")}
                  onClick={() => run(row)}
                >
                  {mine ? spinner : <Library className="h-3.5 w-3.5" />}
                  {t("mb.add_to_library")}
                </button>
              </div>
              {mine ? (
                <div className="text-[11px] text-zinc-500 pb-1">{t("mb.actions_working")}</div>
              ) : answer ? (
                <div className={`text-[11px] pb-1 ${answer.ok ? "text-zinc-500" : "text-amber-500"}`}>
                  {answer.text}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function MBArtistPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { warm: prefetch, cool: unprefetch } = useMbPrefetch();
  // Which release-group TYPE is selected. A chip FILTERS the one discography
  // loaded below; it is not a new question to MusicBrainz. The type of a group
  // is the pair (primary type, secondary types) MusicBrainz reports on every
  // row, so a compound chip like "Album + Live" is answerable from the payload
  // itself — and it can only be answered exactly, because the tail of the list
  // loads EVERY page (see AutoLoad's `all`): an artist's types can sit past the
  // first page, and a filter over a half-loaded discography is what this page
  // used to get wrong.
  const [typeFilter, setTypeFilter] = useState<string>("All");
  const discography = useInfiniteQuery({
    queryKey: ["mbArtist", id],
    queryFn: ({ pageParam }) => api.mbArtist(id, pageParam as number, 300),
    initialPageParam: 0,
    // The server's own next offset (null at the end): it also knows which
    // rows MusicBrainz served, which a client-side row count does not.
    getNextPageParam: (last: MBArtistBrowse) => last.next ?? undefined,
    enabled: !!id,
    placeholderData: keepPreviousData,
  });
  const { isLoading, error } = discography;
  const [mode, setMode] = useState<ImportMode>("best");
  const { busy, run } = useAddToLibrary();
  // No reset-on-id effect: the artist id is a PATH param, and App keys the
  // route subtree by `location.pathname`, so another artist is a fresh mount
  // with the filter already back at "All".

  const groups: RGRow[] = (discography.data?.pages ?? []).flatMap((p) => p.release_groups ?? []);
  const rgTotal: number = discography.data?.pages.at(-1)?.total ?? groups.length;

  // The chips, the sections below them and the "Add or download by release
  // type" panel are ONE derivation over ONE discography: `byReleaseGroupType`
  // names every group's type as MusicBrainz spells a compound one ("Album +
  // Live" — primary type first, then its secondary types) and lists the rows
  // of each. So a chip cannot name a type the panel does not, or the other way
  // round, and "Album + Live" is the live albums only, never every album.
  const sections = byReleaseGroupType(groups);
  const selected = typeFilter === "All" ? sections
    : sections.filter((s) => s.label === typeFilter);
  const shown = selected.flatMap((s) => s.list);

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
        title={withAlias(a.name, a.alias)}
        subtitle={[a.disambiguation, a.type, a.country, life].filter(Boolean).join(" · ")}
        chips={[...(a.genres ?? []), ...(a.tags ?? []).slice(0, 5)].slice(0, 8)}
        actions={
          <>
            {/* One button, one meaning: add the albums in the area the user is
                looking at. With "All" selected that is the whole discography,
                which the server prepares off-request (one MusicBrainz browse
                per release group — `mode` rides along for the API's shared
                shape, and still means one release per group here); with a type
                selected it is exactly the groups on screen. The type rows
                below are the per-TYPE version of the same thing, and both hand
                over the same list: every album is recorded and the search for
                its audio starts as it is added. */}
            <Segmented
              value={mode}
              onChange={setMode}
              options={IMPORT_MODES}
              className={busy ? "opacity-60" : ""}
            />
            <button
              className="btn-primary !py-1.5 text-xs"
              disabled={busy || shown.length === 0}
              title={
                typeFilter === "All"
                  ? "Add one album per release group of this artist to your library — the search for each one starts as it is added"
                  : `Add one album per release group shown (${shown.length}) to your library — the search for each one starts as it is added`
              }
              onClick={() =>
                typeFilter === "All"
                  ? run([String(a.id)], "artist", mode)
                  : run(shown.map((rg) => String(rg.id)), "release_group", mode)
              }
            >
              <AddIcon busy={busy} /> {busy ? "Adding…" : "Add to library"}
            </button>
            <MbHeaderActions
              href={mbUrl("artist", a.id)}
              query={a.name}
            />
            {/* Watching is the third way to get at an artist's releases —
                alongside adding one album and adding the whole discography —
                so it sits with them, on the artist page itself. */}
            <WatchArtistButton artistMbid={String(a.id)} artist={a.name} />
          </>
        }
      />
      <div>
        {groups.length > 0 ? (
          <ArtistTypeActions
            artistId={String(a.id)}
            mode={mode}
            groups={groups}
            total={rgTotal}
            loading={discography.isLoading}
          />
        ) : null}
        {groups.length === 0 ? (
          <EmptyState
            title={typeFilter === "All"
              ? "No release groups on MusicBrainz"
              : `No ${typeFilter} release groups`}
            hint={typeFilter === "All"
              ? undefined
              : "MusicBrainz holds no release group of that type for this artist — switch back to All."}
          />
        ) : (
          <>
            <div className="flex gap-1 flex-wrap mb-4">
              {[{ label: "All", count: rgTotal },
                ...sections.map(({ label, list }) => ({ label, count: list.length }))]
                .map(({ label, count }) => (
                <button
                  key={label}
                  className={`chip px-2.5 py-1 border ${
                    typeFilter === label
                      ? "bg-accent on-accent border-transparent font-semibold"
                      : "bg-raise border-border text-zinc-400 hover:text-white"
                  }`}
                  title={
                    label === "All"
                      ? "Every release group MusicBrainz holds for this artist"
                      : `This artist's ${label} release groups — the same type the panel above adds`
                  }
                  onClick={() => setTypeFilter(label)}
                >
                  {label} ({count})
                </button>
              ))}
            </div>
            {groups.length !== rgTotal && (
              <div className="text-[11px] text-zinc-500 mb-3">
                Showing {groups.length} of {rgTotal}{typeFilter === "All" ? "" : ` ${typeFilter}`} release groups
              </div>
            )}
            {selected.map(({ label, list }) => (
              <div key={label} className="mb-5">
                <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
                  {label} · {list.length}
                </div>
                <div className="rounded-lg border border-border overflow-hidden table-scroll">
                  <div className="stagger">
                    {list.map((rg) => (
                      <div
                        key={rg.id}
                        className="table-row !cursor-pointer"
                        onClick={() => nav(`/mb/rg/${rg.id}`)}
                        onMouseEnter={() => prefetch("release-group", rg.id)}
                        onMouseLeave={unprefetch}
                      >
                        <div className="px-3 py-2 flex items-center gap-3 min-w-0">
                          <span className="text-xs font-mono text-zinc-500 w-10 shrink-0">
                            {(rg.first_release_date || "—").slice(0, 4)}
                          </span>
                          <span className="text-sm text-zinc-200 truncate flex-1">
                            {withAlias(rg.title, rg.alias)}
                            {rg.secondary_types?.length ? (
                              <span className="text-zinc-500 text-xs"> ({rg.secondary_types.join(" + ")})</span>
                            ) : null}
                          </span>
                          <button
                            className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors shrink-0"
                            title="Add this release group to your library and start searching for it"
                            disabled={busy}
                            onClick={(e) => {
                              e.stopPropagation();   // the row itself opens the group
                              run([String(rg.id)], "release_group", mode);
                            }}
                          >
                            <Library className="h-4 w-4" />
                          </button>
                          <ExtLink href={mbUrl("release-group", rg.id)} title="Open on MusicBrainz" />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            ))}
            {/* `all`: the chips above and the panel's rows are derived from
                the groups loaded here, so this list fetches every page rather
                than waiting for the tail to be scrolled into view — an
                artist's compound types can live past the first page. */}
            <AutoLoad
              loaded={groups.length}
              total={rgTotal}
              next={discography.data?.pages.at(-1)?.next}
              busy={discography.isFetchingNextPage}
              hasMore={discography.hasNextPage}
              all
              onLoad={() => discography.fetchNextPage()}
            />
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Countries                                                           */
/* ------------------------------------------------------------------ */

/** Every country a release or release group was released in, with its date —
 *  one chip each, in MusicBrainz's own area names.
 *
 *  A release group is released in as many countries as its editions cover, so
 *  the list is the union the server built for it (each chip naming the edition
 *  that carries that event, which is where a click goes). The user's own
 *  `prefer_release_country` is MARKED, never filtered out: the point of the
 *  list is seeing the whole story, not the policy's pick.
 *
 *  Nothing at all renders without events — a group with no country data has no
 *  field, not an empty label. */
function CountryChips({ events, units }: {
  events?: MBCountryEvent[];
  /** what the header counts — "release"/"edition" of the group being shown */
  units?: string;
}) {
  const nav = useNavigate();
  // A few dozen entries at most: the union is rebuilt per render rather than
  // memoized against an array the caller rebuilds every time anyway.
  const shown = mergeCountries(events ?? []);
  if (!shown.length) return null;
  const preferred = shown.filter((e) => e.preferred).length;
  return (
    <div>
      <div className="text-[11px] uppercase tracking-widest text-zinc-500 mb-1.5">
        Countries · {shown.length}
        {preferred > 0 ? ` · ${preferred} preferred` : ""}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {shown.map((e) => {
          const where = [e.country, e.date].filter(Boolean).join(" · ");
          const title = [
            `${e.country}${e.code ? ` (${e.code})` : ""}`,
            e.date ? `released ${e.date}` : "",
            e.preferred ? "your preferred release country" : "",
            e.release_id && units ? `carried by this ${units} — open it` : "",
          ].filter(Boolean).join(" — ");
          const chip = (
            <span className={`chip border ${e.preferred
              ? "bg-accent/15 border-accent/40 text-accent-soft"
              : "bg-raise border-border text-zinc-300"}`}>
              {e.preferred && <Check className="h-3 w-3 shrink-0" />}
              {where}
            </span>
          );
          return e.release_id ? (
            <button
              key={`${e.release_id}|${e.code}|${e.country}|${e.date}`}
              title={title}
              onClick={() => nav(`/mb/release/${e.release_id}`)}
              className="tap"
            >
              {chip}
            </button>
          ) : (
            <span key={`${e.code}|${e.country}|${e.date}`} title={title}>{chip}</span>
          );
        })}
      </div>
    </div>
  );
}

/** The union of the country lists of the pages loaded so far: a release group
 *  pages its editions, and each page's list covers its own window, so the
 *  chips have to add up or they would disagree with the table underneath.
 *  Same rule as the server's own union — one entry per (country, date), the
 *  first edition that carries it wins — so the two cannot drift. */
function mergeCountries(events: MBCountryEvent[]): MBCountryEvent[] {
  const seen = new Set<string>();
  const out: MBCountryEvent[] = [];
  for (const e of events) {
    const key = `${e.code || e.country}|${e.date}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(e);
  }
  return out.sort((a, b) =>
    (a.date || "9999").localeCompare(b.date || "9999") || a.country.localeCompare(b.country));
}

/* ------------------------------------------------------------------ */
/* Release group                                                       */
/* ------------------------------------------------------------------ */

export function MBReleaseGroupPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { warm: prefetch, cool: unprefetch } = useMbPrefetch();
  const editions = useInfiniteQuery({
    queryKey: ["mbRG", id],
    queryFn: ({ pageParam }) => api.mbReleaseGroup(id, pageParam as number),
    initialPageParam: 0,
    getNextPageParam: (last: MBReleaseGroupBrowse) => last.next ?? undefined,
    enabled: !!id,
    placeholderData: keepPreviousData,
  });
  const { isLoading, error } = editions;
  const releasesAll: RelRow[] = (editions.data?.pages ?? []).flatMap((p) => p.releases ?? []);
  // Each page's country list covers its own window of editions, so the chips
  // read the union of the pages loaded here — the same list the table below
  // sums up to.
  const countries = (editions.data?.pages ?? []).flatMap((p) => p.countries ?? []);
  const relTotal: number = editions.data?.pages.at(-1)?.total ?? releasesAll.length;
  const { sort, onSort, sorted } = useSort(releasesAll, "date");
  // Bulk auto-import is the whole point of this page, so its editions table
  // carries the library's select-mode: checkbox column, selected rows tinted
  // and a batch bar above the table.
  const [sel, setSel] = useState<string[]>([]);
  const [mode, setMode] = useState<ImportMode>("best");
  // The edition the user forced ("" = the policy's own pick). Owned here
  // because the add call carries the same id: the panel and the button must
  // never disagree about which edition the app is about to fetch.
  const [edition, setEdition] = useState("");
  const { busy, run } = useAddToLibrary();
  const toggleSel = (rid: string) =>
    setSel((s) => (s.includes(rid) ? s.filter((x) => x !== rid) : [...s, rid]));
  // rows the payload could not identify still count as selected, so the batch
  // reports them as skipped instead of queueing `undefined`
  const selIds = sel.filter((x) => x && x !== "undefined");

  // Owned = the library already holds an album tagged with this group, so the
  // page points at it instead of offering to download it a second time.
  const ownedGroup = useOwnedAlbum([editions.data?.pages[0]?.id]);

  if (!id) return null;
  if (isLoading) return <PageLoading label="Asking MusicBrainz…" />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  const rg = editions.data?.pages[0];
  if (!rg) return null;
  const typeLabel = [rg.primary_type, ...(rg.secondary_types ?? [])].filter(Boolean).join(" + ");

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        back={{ to: "/mb/search", label: "MusicBrainz search" }}
        overline="MusicBrainz release group"
        title={withAlias(rg.title, rg.alias)}
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
            {ownedGroup && (
              <Link
                className="btn-ghost !py-1.5 text-xs !text-emerald-300"
                to={albumRef(ownedGroup)}
                title="The library already holds this release group — open the local album"
              >
                <Check className="h-3.5 w-3.5" /> In library
              </Link>
            )}
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
                mode === "all"
                  ? "Add every eligible edition of this group to your library and start searching for them"
                  : edition
                    ? "Add the edition you picked to your library and start searching for it"
                    : "Add the edition the policy prefers for this group to your library and start searching for it"
              }
              onClick={() =>
                run(
                  [String(rg.id)],
                  "release_group",
                  mode,
                  0,
                  // The override is deliberately not sent with mode "all":
                  // the pick is one edition, and "all" means every eligible one.
                  mode === "best" && edition ? { release_mbid: edition } : {}
                )
              }
            >
              <AddIcon busy={busy} /> {busy ? "Adding…" : "Add to library"}
            </button>
            <MbHeaderActions
              href={mbUrl("release-group", rg.id)}
              query={[rg.artist, rg.title].filter(Boolean).join(" ")}
            />
          </>
        }
      >
        {/* Beside the Add-to-library button, and the override it reports is
            the release_mbid that button sends. */}
        <ReleaseChoice
          releaseGroupMbid={String(rg.id)}
          override={edition}
          onOverride={(mbid) => setEdition(mbid)}
        />
      </PageHeader>
      <CountryChips events={countries} units="edition" />
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
                onClick={() => run(selIds, "release", "best", sel.length - selIds.length)}
                title="Add the selected editions to your library and start searching for them"
              >
                <AddIcon busy={busy} /> {busy ? "Adding…" : "Add to library"}
              </button>
              <button className="btn-ghost !py-1 text-xs" onClick={() => setSel([])}>
                Clear
              </button>
            </div>
          </div>
        )}
        <div className={`rounded-lg border border-border overflow-hidden transition-opacity ${editions.isPlaceholderData ? "opacity-50" : ""}`}>
          <div className="table-scroll">
            <table className={`${TABLE_FIT} text-sm`}>
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
                  <SortTh label="Title" k="title" sort={sort} onSort={onSort} className="w-[240px]" />
                  <th className="th w-[120px]">Format</th>
                  <SortTh label="Discs" k="disc_count" sort={sort} onSort={onSort} className="w-[64px] cell-nowrap text-right" />
                  <SortTh label="Tracks" k="track_count" sort={sort} onSort={onSort} className="w-[96px] cell-nowrap text-right" />
                  <th className="th w-[72px]">Country</th>
                  <th className="th w-[150px]">Barcode</th>
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
                    onMouseLeave={unprefetch}
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
                      <span className="truncate">{withAlias(r.title, r.alias)}</span>
                      {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
                    </td>
                    <td className="td text-zinc-500">
                      <span className="inline-flex flex-wrap items-center gap-1">
                        <StatusBadge status={r.status} formats={r.formats} />
                      </span>
                    </td>
                    <td className="td text-zinc-500 text-right">{r.disc_count || "—"}</td>
                    <td
                      className="td text-zinc-500 text-right tabular-nums cell-ellipsis"
                      title={(r.disc_count ?? 1) > 1 ? `${r.track_count} tracks across ${r.disc_count} discs — ${r.track_breakdown}` : undefined}
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
          <AutoLoad
            loaded={releasesAll.length}
            total={relTotal}
            next={editions.data?.pages.at(-1)?.next}
            busy={editions.isFetchingNextPage}
            hasMore={editions.hasNextPage}
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
  const { data: r, isLoading, error } = useQuery({
    queryKey: ["mbRelease", id],
    queryFn: () => api.mbRelease(id),
    enabled: !!id,
  });
  const [mode, setMode] = useState<ImportMode>("best");
  const { busy, run } = useAddToLibrary();
  // The release, or failing that its group, is already on disk: the header
  // links to the local album rather than pretending this is new music.
  const owned = useOwnedAlbum([r?.id, r?.release_group_id]);
  if (isLoading) return <PageLoading label="Asking MusicBrainz…" />;
  if (error) return <div className="p-6"><LoadError e={error} /></div>;
  if (!r) return null;

  const cover = `https://coverartarchive.org/release/${r.id}/front-500`;
  const artist = r.artists?.map((a) => a.name).join(", ") || "";
  const artistMbid = r.artists?.[0]?.mbid || r.artist_mbid || "";
  const meta = [
    typeLabel(r.primary_type, r.secondary_types) || r.release_type,
    r.date,
    [r.label, r.catalog_number].filter(Boolean).join(" · "),
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
        title={withAlias(r.title, r.alias)}
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
              <StatusBadge status={r.status} formats={r.medium} />
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
                  ? "Add every eligible edition of this release group to your library and start searching for them"
                  : "Add this exact release to your library and start searching for it"
              }
              onClick={() =>
                run([importTarget.mbid], importTarget.kind, mode, 0, {
                  title: r.title, artist, year: (r.date || "").slice(0, 4),
                })
              }
            >
              <AddIcon busy={busy} /> {busy ? "Adding…" : "Add to library"}
            </button>
            {owned && (
              <Link
                className="btn-ghost !py-1.5 text-xs !text-emerald-300"
                to={albumRef(owned)}
                title="The library already holds this release — open the local album"
              >
                <Check className="h-3.5 w-3.5" /> In library
              </Link>
            )}
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

      {/* Every country this pressing was released in, with its date — the
          header's single country code was only MusicBrainz's first event. */}
      <CountryChips events={r.countries} />

      <div className="grid grid-cols-1 lg:grid-cols-[240px_1fr] gap-6">
        <div>
          <img
            src={api.artUrl(cover)}
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
                      <button
                        className="p-1 rounded-lg text-zinc-600 hover:text-white hover:bg-raise transition-colors shrink-0"
                        title="Add this track's release to your library and start searching for it"
                        disabled={busy}
                        onClick={() =>
                          run([t.recording_mbid as string], "recording", "best", 0, {
                            release_mbid: r.id, title: r.title, artist,
                            year: (r.date || "").slice(0, 4),
                          })
                        }
                      >
                        <Library className="h-3.5 w-3.5" />
                      </button>
                    )}
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
  const { warm: prefetch, cool: unprefetch } = useMbPrefetch();
  const appearances = useInfiniteQuery({
    queryKey: ["mbRecording", id],
    queryFn: ({ pageParam }) => api.mbRecording(id, pageParam as number),
    initialPageParam: 0,
    getNextPageParam: (last: MBRecordingBrowse) => last.next ?? undefined,
    enabled: !!id,
    placeholderData: keepPreviousData,
  });
  const { isLoading, error } = appearances;
  const releasesAll: RelRow[] = (appearances.data?.pages ?? []).flatMap((p) => p.releases ?? []);
  const relTotal: number = appearances.data?.pages.at(-1)?.total ?? releasesAll.length;
  const { sort, onSort, sorted } = useSort(releasesAll, "date");
  const { busy, run } = useAddToLibrary();

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
            {/* A recording is a track, and an album is what the library holds:
                this adds the RELEASE the track appears on — the best edition
                when none of the rows below has been picked. */}
            <button
              className="btn-primary !py-1.5 text-xs"
              disabled={busy}
              title="Add the release this track appears on to your library and start searching for it"
              onClick={() =>
                run([r.id], "recording", "best", 0, {
                  title: r.title, artist: r.artist || "",
                })
              }
            >
              <AddIcon busy={busy} /> {busy ? "Adding…" : "Add to library"}
            </button>
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
            <table className={`${TABLE_FIT} text-sm`}>
              <thead className="border-b border-border">
                <tr>
                  <SortTh label="Date" k="date" sort={sort} onSort={onSort} className="w-24 cell-nowrap" />
                  <SortTh label="Title" k="title" sort={sort} onSort={onSort} className="w-[240px]" />
                  <th className="th w-[120px]">Format</th>
                  <th className="th w-[110px]">Type</th>
                  <SortTh label="Tracks" k="track_count" sort={sort} onSort={onSort} className="w-[80px] text-right" />
                  <th className="th w-[72px]">Country</th>
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
                    onMouseLeave={unprefetch}
                  >
                    <td className="td text-zinc-500 cell-nowrap">{rel.date || "—"}</td>
                    <td className="td text-zinc-200">
                      <span className="truncate">{withAlias(rel.title, rel.alias)}</span>
                    </td>
                    <td className="td text-zinc-500">
                      <span className="inline-flex flex-wrap items-center gap-1">
                        <StatusBadge status={rel.status} formats={rel.formats} />
                      </span>
                    </td>
                    <td className="td text-zinc-500 truncate">
                      {typeLabel(rel.primary_type, rel.secondary_types) || "—"}
                    </td>
                    <td
                      className="td text-zinc-500 text-right tabular-nums cell-ellipsis"
                      title={(rel.disc_count ?? 1) > 1 ? `${rel.track_count} tracks across ${rel.disc_count} discs — ${rel.track_breakdown}` : undefined}
                    >
                      {tracksLabel(rel) || "—"}
                    </td>
                    <td className="td text-zinc-500">{rel.country || "—"}</td>
                    <td className="td w-10 pr-2">
                      <span className="inline-flex items-center">
                        <button
                          className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors"
                          title={`Add “${rel.title}” to your library and start searching for it`}
                          disabled={busy}
                          onClick={(e) => {
                            e.stopPropagation();   // the row itself opens the release
                            run([rel.id], "release", "best", 0, {
                              title: rel.title || "", artist: r.artist || "",
                              year: (rel.date || "").slice(0, 4),
                            });
                          }}
                        >
                          <Library className="h-3.5 w-3.5" />
                        </button>
                        <ExtLink href={mbUrl("release", rel.id)} title="Open on MusicBrainz" />
                      </span>
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
          <AutoLoad
            loaded={releasesAll.length}
            total={relTotal}
            next={appearances.data?.pages.at(-1)?.next}
            busy={appearances.isFetchingNextPage}
            hasMore={appearances.hasNextPage}
            onLoad={() => appearances.fetchNextPage()}
          />
        </div>
      </div>
    </div>
  );
}
