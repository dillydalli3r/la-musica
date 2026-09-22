import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  UploadCloud, ExternalLink, Check, ChevronLeft, ChevronRight, ChevronDown, Wand2,
  Plus, Trash2, Disc3, FolderOpen, X, Search, Loader2, Image as ImageIcon, AlertTriangle,
  Languages, RotateCcw,
} from "lucide-react";
import { api, answerSources, replyFor, IN_MOBILE_SHELL } from "../api";
import type { AdvisoryFetchResult, MetadataFetchItem, MetadataItemKind } from "../api";
import { toast, useStore } from "../store";
import { advisoryLine, advisoryOutcome } from "../components/Badges";
import { LinkValidChip } from "../components/Links";
import LyricsViewer, { parseLrc } from "../components/LyricsViewer";
import CoverSearchModal from "../components/CoverSearchModal";
import CoverImg, { TrackCover } from "../components/CoverImg";
import PageHeader from "../components/PageHeader";
import { useI18n } from "../lib/i18n";
import MetadataReviewModal from "../components/MetadataReviewModal";
import type {
  AcoustidAlbumMatch, AcoustidMatch, AcoustidSubmitResult, AcoustidWrite, CoverResult,
  ImportBulkJob, ImportPrompt, ImportScriptsPreview, LyricsAutoResult, MBRelease, MatchSuggestion,
  ScriptRunResult, Track,
} from "../types";
import { DEFAULT_RUN_ALL, SCRIPT_LABEL, isScriptId } from "../lib/scripts";
import { fmtCounts, fmtSteps } from "../lib/fmt";
import { GENRE_COUNT_MAX, GENRE_FAMILIES, canonicalGenre, familyOf, splitGenres } from "../lib/genres";

const STEPS = ["Select & separate", "Links", "Match", "Covers", "Genres", "Lyrics", "Advisory", "Finish"];

/** THE row recipe of the wizard.
 *
 *  Every list row of the album's own items — the staged files, the release
 *  hits, the matches, the per-track covers, genres, lyrics and advisory rows,
 *  and the queue's albums — is this one string, so one step's rows cannot be
 *  taller, denser or differently framed than another's. The steps used to
 *  build their own: a file row was `px-2 py-1` with no frame where a track row
 *  was `panel px-3 py-2`, and the same album read as two different lists from
 *  one step to the next. */
const ROW = "flex items-center gap-3 panel px-3 py-2";
/** The same row for the steps whose rows carry several controls: they wrap on
 *  a narrow window instead of overflowing. Padding, frame and type sizes stay
 *  the row's own — only the height follows the content. */
const ROW_WRAP = `${ROW} flex-wrap`;

/** The wizard step each import family lives on, and the words for it — the
 *  same five families the server's own registry names (mlo/import_policy
 *  .FAMILIES), looked up by step NAME so a step a later edit inserts cannot
 *  land a prompt's link on the wrong one. */
const FAMILY_STEP: Record<string, string> = {
  links: "Links",
  cover: "Covers",
  genres: "Genres",
  lyrics: "Lyrics",
  advisory: "Advisory",
};

const FAMILY_LABEL: Record<string, string> = {
  links: "Links",
  cover: "Cover art",
  genres: "Genres",
  lyrics: "Lyrics",
  advisory: "Advisory",
};

const FAMILY_ORDER = Object.keys(FAMILY_STEP);

/** The family ids a `?missing=` param names, in wizard order. */
function missingFromParam(raw: string | null): string[] {
  return (raw ?? "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter((id) => id in FAMILY_STEP)
    .sort((a, b) => FAMILY_ORDER.indexOf(a) - FAMILY_ORDER.indexOf(b));
}

/** The step a `?step=` param asks for: a family id ("cover"), a step name
 *  ("Covers") or an index. Null when it names nothing — then the first missing
 *  family decides, and failing that the album's own default. */
function stepFromParam(raw: string | null, missing: string[]): number | null {
  const value = (raw ?? "").trim();
  // Whatever the param spells, the step it lands on comes from the registry
  // above — never from a number a reordered STEPS list would invalidate.
  const wanted = value
    ? FAMILY_STEP[value.toLowerCase()] ?? value
    : missing.length
      ? FAMILY_STEP[missing[0]]
      : "";
  const byName = wanted ? STEPS.findIndex((s) => s.toLowerCase() === wanted.toLowerCase()) : -1;
  if (byName >= 0) return byName;
  const index = Number(value);
  return value && Number.isInteger(index) && index >= 0 && index < STEPS.length ? index : null;
}

/** Minimum entry mode — the wizard opened from a prompt's own link
 *  (`/import?album=…&step=…&missing=cover,advisory`), which is what the queue
 *  and the notification hand the user: "this album still needs these", not
 *  "here is the whole step's form".
 *
 *  `min` marks such a visit, `here` is the family the step the user landed on
 *  is missing (null when this step has nothing missing), and `mine` is the
 *  family a block of the step belongs to. In an ordinary visit every block
 *  renders exactly where it sits. In a minimum visit the block for `here` is
 *  the visible step and every other block of that step is one
 *  collapse-until-asked disclosure; `order-last` keeps the disclosure after
 *  the controls whatever order the source is in (the step container is a
 *  column flex box in this mode, see the step markup), so the user reads what
 *  they were sent for first.
 *
 *  Each block is wrapped exactly ONCE per step, so a control belonging to the
 *  family can never be rendered twice — the disclosure holds what the controls
 *  do not, never a second copy of them. */
function MinBlock({ min, here, mine, children }: {
  min: boolean; here: string | null; mine: string; children: ReactNode;
}) {
  if (!children) return null;
  // The three cases that render as usual: an ordinary visit, the block the
  // visit is about, and a step with nothing missing — there is no question to
  // put to the user there, so nothing is taken away from them either (the step
  // says as much in MinNothingMissing).
  if (!min || !here || mine === here) return <>{children}</>;
  return (
    <details className="order-last rounded-lg border border-border bg-zinc-950/40 px-3 py-2">
      <summary className="text-xs font-medium cursor-pointer text-zinc-400 select-none">
        Show everything else on this step
      </summary>
      <div className="mt-2 space-y-3">{children}</div>
    </details>
  );
}

/** What a minimum visit gets on a step with nothing missing: the step is not
 *  empty, it is simply not being asked for — so say that, instead of showing a
 *  form the visit is not about. Nothing is hidden from the user here. */
function MinNothingMissing() {
  return (
    <div className="rounded-lg border border-border bg-zinc-950/40 px-3 py-2 text-xs text-zinc-500">
      Nothing is missing on this step for this album — nothing here is waiting
      for you, and you can leave it as it is.
    </div>
  );
}

/** The lyrics step's status words. The provider chain reports "ok"/"skipped"/
 *  "failed"; a chip is a label and reads as one. */
const LYR_STATUS_LABEL: Record<string, string> = { ok: "OK", skipped: "Skipped", failed: "Failed" };

/** The queue panel's per-album state words. The bulk job reports lowercase
 *  machine states; a chip is a label and reads as one. */
const QUEUE_STATE_LABEL: Record<string, string> = {
  queued: "Queued", running: "Running", imported: "Imported", skipped: "Skipped", failed: "Failed",
};

// Everything the importer accepts: audio, all common image formats, and the
// sidecars the optimizer understands (.lrc, .cue, .log, .accurip).
const ALLOWED = /\.(flac|mp3|m4a|mp4|ogg|opus|wav|aac|wv|ape|alac|aiff|aif|dsf|dff|mka)$|\.(jpg|jpeg|png|webp|bmp|gif|tiff|tif|avif|heic|heif|jxl|svg)$|\.(lrc|cue|log|accurip)$/i;
const AUDIO_RE = /\.(flac|mp3|m4a|mp4|ogg|opus|wav|aac|wv|ape|alac|aiff|aif|dsf|dff|mka)$/i;
const DISC_RE = /^(cd|disc|disk)\s*\d+$/i;

interface ImportFile {
  file: File | null; // null = native pick (already on disk)
  relPath: string;
}

interface AlbumGroup {
  name: string;
  root: string; // rel path of the album dir under the drop/native root ("" = root itself)
  files: ImportFile[];
}

/** One track of `/api/album/scan-tracks` — the folder-scan payload the wizard
 *  falls back on for an album the library tree does not list yet. */
interface ScanTrackRow {
  path: string;
  file: string;
  tracknumber?: number | null;
  discnumber?: number | null;
  lyrics_embedded?: boolean;
  lyrics_lrc?: boolean;
  lyrics_present?: boolean;
  tech?: Track["tech"];
  tags?: Track["tags"];
}

/** One script's outcome in the Finish step's report — a failing or skipped
 *  script is a row, not a toast that has already faded. */
interface RunRow {
  id: number;
  label: string;
  ok: boolean;
  skipped: boolean;
  error?: string;
  note?: string;
}

function dirOf(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i === -1 ? "" : relPath.slice(0, i);
}

function baseName(p: string): string {
  const parts = p.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

/** Is `p` inside the configured music folder? Compared the way the server
 *  compares it — normalized separators, case-insensitively, at directory
 *  boundaries. The wizard asks the server for its own album folder either
 *  way; this only decides whether that needs the staged allowance. */
function inMusicFolder(p: string, folder: unknown): boolean {
  const f = String(folder ?? "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const q = p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  return !!f && (q === f || q.startsWith(`${f}/`));
}

/** Extract a MusicBrainz ID from an ID or a musicbrainz.org URL. */
function extractMbid(value: string): string | null {
  const m = value.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  return m ? m[0].toLowerCase() : null;
}

/** One release hit, as `/api/mb/search/releases` renders it. */
type MBSearchHit = { id: string; title?: string; artist?: string; date?: string; catalog_number?: string; barcode?: string };

/** Is `v` one of those hits? The endpoint answers with the raw MusicBrainz
 *  shape, so the one field the wizard needs is checked rather than assumed. */
function isSearchHit(v: unknown): v is MBSearchHit {
  if (!v || typeof v !== "object" || !("id" in v)) return false;
  return typeof v.id === "string";
}

/** A name folded to what a match compares: lowercase, accents stripped, every
 *  run of punctuation and spacing collapsed — “System of a Down” and
 *  “system of a down!” fold together, “Söme” and “Some” too. */
function matchKey(s: string): string {
  return s
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/** Retry a transient network failure (MusicBrainz rate limits / blips). */
async function withRetry<T>(fn: () => Promise<T>, tries = 3, baseDelay = 1200): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i < tries; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      if (i < tries - 1) await new Promise((r) => setTimeout(r, baseDelay * (i + 1)));
    }
  }
  throw lastErr;
}

/** Parse a disc number from a tag like "1", "1/2" or "Disc 2". */
function parseDisc(v: string | null | undefined): number | null {
  if (!v) return null;
  const m = String(v).trim().match(/(\d+)/);
  const n = m ? parseInt(m[1], 10) : NaN;
  return Number.isNaN(n) ? null : n;
}

/** Group rows into per-disc sections (unmatched rows last). */
function groupByDisc<T>(rows: T[], discOf: (r: T) => number | null): { disc: number | null; rows: T[] }[] {
  const groups = new Map<number | null, T[]>();
  for (const r of rows) {
    const d = discOf(r);
    if (!groups.has(d)) groups.set(d, []);
    groups.get(d)!.push(r);
  }
  return [...groups.keys()]
    .sort((a, b) => {
      if (a === null) return 1;
      if (b === null) return -1;
      return a - b;
    })
    .map((d) => ({ disc: d, rows: groups.get(d)! }));
}

/** Detect album boundaries inside an import set.
 *  An album = any dir with immediate audio children (disc-like dirs such as
 *  CD1/Disc 2 merge into their parent). Group names fall back to
 *  "Parent - Album" when albums sit inside artist folders. */
function detectAlbums(imports: ImportFile[], fallbackName: string): AlbumGroup[] {
  const audio = imports.filter((f) => AUDIO_RE.test(f.relPath));
  if (!audio.length) {
    return [{ name: fallbackName || "New Album", root: "", files: [...imports] }];
  }
  const dirsWithAudio = new Set(audio.map((f) => dirOf(f.relPath)));

  const isDiscDir = (d: string) => d !== "" && DISC_RE.test(baseName(d));
  const roots = new Set<string>();
  for (const d of dirsWithAudio) {
    if (!d) {
      roots.add("");
      continue;
    }
    if (isDiscDir(d)) {
      const parent = dirOf(d);
      const siblings = [...dirsWithAudio].filter((x) => x && dirOf(x) === parent);
      if (siblings.length && siblings.every((s) => isDiscDir(s))) {
        roots.add(parent || "");
      } else {
        roots.add(d);
      }
    } else {
      roots.add(d);
    }
  }
  if (!roots.size) roots.add("");

  const ordered = [...roots].sort((a, b) => {
    const da = a ? a.split("/").length : 0;
    const db = b ? b.split("/").length : 0;
    return da - db || a.localeCompare(b);
  });

  // The wrapper folder the user dropped/picked (e.g. "Rips/"). Albums sitting
  // directly inside it are named by their own folder only.
  const firstSeg = imports[0]?.relPath.split("/")[0] ?? "";
  const allShare = imports.every((f) => f.relPath.split("/")[0] === firstSeg);
  const dropRoot = firstSeg && allShare ? firstSeg : "";

  const nameFor = (d: string): string => {
    if (!d) return fallbackName || "New Album";
    const parent = dirOf(d);
    const base = baseName(d);
    if (!parent || parent === dropRoot) return base;
    return `${baseName(parent)} - ${base}`;
  };

  const groups = new Map<string, ImportFile[]>();
  const rootsDeep = [...roots].sort((a, b) => b.split("/").length - a.split("/").length);
  for (const f of imports) {
    const fd = dirOf(f.relPath);
    const root = rootsDeep.find((r) => r === "" || fd === r || fd.startsWith(`${r}/`)) ?? ordered[0];
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root)!.push(f);
  }

  return ordered.map((r) => ({
    name: nameFor(r),
    root: r,
    files: groups.get(r) ?? [],
  }));
}

export default function ImportWizard() {
  const { t } = useI18n();
  const [params, setParams] = useSearchParams();
  const albumParam = params.get("album");
  const initialAlbum = albumParam ?? null;
  // A prompt's own link opens the album AT the step that needs a decision
  // (`?album=…&step=Covers&missing=cover,advisory`): the step is where the
  // user lands, and the missing families ride along so every step can say
  // what is still open on this album.
  const paramMissing = missingFromParam(params.get("missing"));
  const [missingFamilies, setMissingFamilies] = useState<string[]>(paramMissing);
  const [step, setStep] = useState(
    () => stepFromParam(params.get("step"), paramMissing) ?? (initialAlbum ? 1 : 0)
  );

  const [albumPath, setAlbumPath] = useState<string | null>(initialAlbum);
  const [albumName, setAlbumName] = useState("");
  const [source, setSource] = useState<"web" | "native">("web");
  const [nativeRoot, setNativeRoot] = useState<string | null>(null);
  const [albums, setAlbums] = useState<AlbumGroup[]>([]);
  const [mediaType, setMediaType] = useState<string>(""); // "" unsure | "CD" | "Digital Media"
  const [uploaded, setUploaded] = useState<{ name: string; path: string }[]>([]);
  const [albumIndex, setAlbumIndex] = useState(0);
  const [uploading, setUploading] = useState(false);
  // relPaths the user unticked: PARTIAL import. Excluded files are never
  // uploaded or moved, and the release tracklist recorded at match time lets
  // the album page grey out exactly those tracks.
  const [excluded, setExcluded] = useState<Set<string>>(new Set());

  const [mbLink, setMbLink] = useState("");
  const [rymLink, setRymLink] = useState("");
  const [rymValid, setRymValid] = useState<boolean | null>(null);
  // Which page /api/rym/validate recognized: only an album page belongs in
  // RATEYOURMUSIC_ALBUM (a song/artist page stored there looks resolved
  // forever and blocks the automatic album lookup).
  const [rymKind, setRymKind] = useState<string | null>(null);
  const [rymNote, setRymNote] = useState("");
  const [rymArtistLink, setRymArtistLink] = useState("");
  // The artist field's own verdict, the same way the album field keeps one:
  // only an ARTIST page may be written as RATEYOURMUSIC_ARTIST.
  const [rymArtistValid, setRymArtistValid] = useState<boolean | null>(null);
  const [rymArtistKind, setRymArtistKind] = useState<string | null>(null);
  const [rymArtistNote, setRymArtistNote] = useState("");
  const [findingLinks, setFindingLinks] = useState(false);
  const [detectedFromTags, setDetectedFromTags] = useState(false);
  const [mbSearch, setMbSearch] = useState("");
  const [searchHits, setSearchHits] = useState<any[]>([]);
  const [release, setRelease] = useState<MBRelease | null>(null);
  const [releaseId, setReleaseId] = useState("");
  const [suggestions, setSuggestions] = useState<MatchSuggestion[]>([]);
  // Per-track genre LIST (the derived family first, the specific genres after
  // it) — a track's GENRE tag is repeated fields, so a joined string here
  // would collapse three genres into one tag on save.
  const [genres, setGenres] = useState<Record<string, string[]>>({});
  const [discGenres, setDiscGenres] = useState<Record<number, string>>({});
  const [genreAddValues, setGenreAddValues] = useState<Record<string, string>>({});
  /** Per-run import cap: which `limit` the next source run passes. Never
   *  above `mb_genre_count` — the server's own cap is what gets written. */
  const [genreLimit, setGenreLimit] = useState<number | null>(null); // null = the configured cap
  /** What the last per-source genre import answered: how many tracks that
   *  source updated, the names it wrote, and its own note when it stayed
   *  silent (a blocked RateYourMusic says so here). */
  const [genreJobResult, setGenreJobResult] = useState<{
    updated: number;
    genres: string[];
    per_source: Record<string, string[]>;
    notes: Record<string, string>;
    /** Genres per track (`mb_genre_count`) the run applied, and how many
     *  tracks it trimmed to reach it. */
    genre_count: number;
    trimmed: number;
  } | null>(null);
  // Last MusicBrainz genre import failure — the 400 that names the missing
  // MBID, kept in the step instead of only in a toast.
  const [genreError, setGenreError] = useState<string | null>(null);
  const [collapsedDiscs, setCollapsedDiscs] = useState<Set<number | null>>(new Set());
  const [advisory, setAdvisory] = useState<Record<string, string>>({});
  // Outcome of the Advisory step's own auto-import: the per-track value and
  // who stated it (`values`/`sources`/`answers`), or the server's error text.
  const [advReply, setAdvReply] = useState<AdvisoryFetchResult | null>(null);
  const [advError, setAdvError] = useState<string | null>(null);
  const [instrumental, setInstrumental] = useState<Record<string, string>>({});
  const [lyricsDrafts, setLyricsDrafts] = useState<Record<string, string>>({});
  /** Tracks whose lyrics editor is open. One row per track stays one line;
   *  the editor is behind an explicit Edit. */
  const [lyrOpen, setLyrOpen] = useState<Set<string>>(new Set());
  const toggleLyricsRow = (path: string) =>
    setLyrOpen((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  // Covers step: album/per-track cover feedback + the per-track selection.
  const [coverNotice, setCoverNotice] = useState<string | null>(null);
  const [lyricsNotice, setLyricsNotice] = useState<string | null>(null);
  const [coverSel, setCoverSel] = useState<Set<string>>(new Set());
  const [coverUrl, setCoverUrl] = useState("");
  const [trackCoverUrl, setTrackCoverUrl] = useState("");
  // Cover finder in the Covers step: null = closed, else the candidates it
  // opens on (empty = search from scratch, staged rows = the import's picks).
  const [coverSearch, setCoverSearch] = useState<{ results?: CoverResult[]; provider?: string | null } | null>(null);
  const albumCoverInput = useRef<HTMLInputElement>(null);
  const trackCoverInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [fetchStatus, setFetchStatus] = useState<string | null>(null);
  // ---- progress for EVERY action in the wizard --------------------------
  // `busy` alone disabled the buttons and left the step looking frozen. The
  // relay's own frame (the websocket the header bar draws, fed by script and
  // import runs) covers what the engine publishes; `act` names what is running
  // and, when the action counts its own steps, its count. An action that
  // reports neither still gets a moving indeterminate bar plus the clock.
  // A per-slice selector, like the library page: a bare useStore() would
  // re-render every step of the wizard on playback/queue/toast writes too.
  const progress = useStore((s) => s.progress);
  const [act, setAct] = useState<{ label: string; kind?: "metadata"; done?: number; total?: number } | null>(null);
  const qc = useQueryClient();

  // ---- Bulk queue (several albums at once) ------------------------------
  // More than one album selected/dropped switches the wizard into queue mode:
  // the 8 steps below keep working on the album picked in the dropdown, the
  // queue itself is imported and post-processed through api.importBulk.
  const [bulkJob, setBulkJob] = useState<ImportBulkJob | null>(null);
  const queueMode = uploaded.length > 1 || albums.length > 1;
  const [acoustid, setAcoustid] = useState<AcoustidMatch | null>(null);
  const [acoustidBusy, setAcoustidBusy] = useState(false);
  // Albums whose accepted match was WRITTEN into the files (ACOUSTID_ID +
  // ACOUSTID_FINGERPRINT). Only those can be submitted to AcoustID — the
  // submission reads the pair back off the files — so the block offers the
  // action per applied row and nothing else. Cleared by a new fingerprint run,
  // whose rows have not been applied yet.
  const [acoustidApplied, setAcoustidApplied] = useState<Record<string, true>>({});
  const [matchAllBusy, setMatchAllBusy] = useState(false);
  // Per-track results of the last lyrics auto-import (provider per track).
  const [lyrResults, setLyrResults] = useState<Record<string, LyricsAutoResult>>({});
  // Outcome of the lyrics chain's other two halves — the transliteration pass
  // (script 17) and the LRCLIB publish (script 18) — as the pass itself
  // reported it. `failed` is what colours the line, not what decided it.
  const [lyrPass, setLyrPass] = useState<
    { kind: "xlit" | "publish"; text: string; failed: boolean } | null
  >(null);

  // Exactly which scripts the import chain runs (Settings → Import).
  const { data: scriptChain } = useQuery({
    queryKey: ["importScripts"],
    queryFn: () => api.importScriptsPreview(),
  });

  // Albums an import could not finish by itself, raised by
  // server.imports.finish_album (one entry per album, with the wizard link
  // that lands on it at the step needing a decision). The same list the
  // notification announces, so the decision is one click from the step.
  const { data: promptData } = useQuery({
    queryKey: ["importPrompts"],
    queryFn: api.importPrompts,
    staleTime: 15_000,
  });
  const importPrompts = promptData?.prompts ?? [];

  // Poll the bulk queue while it runs; done/failed stops the poll.
  useEffect(() => {
    if (bulkJob?.status !== "running") return;
    const timer = setInterval(() => {
      api.importBulkStatus()
        .then((job) => {
          setBulkJob(job);
          if (job.status !== "running") qc.invalidateQueries({ queryKey: ["library"] });
        })
        .catch(() => setBulkJob(null)); // server restarted: nothing to poll
    }, 1500);
    return () => clearInterval(timer);
  }, [bulkJob?.status, qc]);

  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });

  // Metadata review is off unless the config explicitly turns it on.
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.config });
  /** The album is NOT in the library yet (a finished download, a folder the
   *  user pointed the wizard at). Every path-taking call below passes this, so
   *  the server's opt-in staged allowance covers this album — and only it. */
  const staged = !!albumPath && !inMusicFolder(albumPath, cfg?.music_folder);
  /** Album folder whose metadata review modal is open (metadata_review only). */
  const [reviewPath, setReviewPath] = useState<string | null>(null);

  // ---- artist image / artist description / album description -----------
  // What an import owes besides the audio; the artwork step accounts for all
  // three. The artist is resolved by NAME exactly like the fetch route does
  // (mlo.artistdata.artist_dir), so a row and a fetch can never disagree about
  // which folder they mean; the payload's `path` says where it landed. Album
  // description comes from the album payload's own `artwork` block. The key
  // matches the artist page's data, so both share one cache entry.
  const { data: albumDetail, refetch: refetchAlbumDetail } = useQuery({
    queryKey: ["album", albumPath],
    queryFn: () => api.album(albumPath!, staged),
    enabled: !!albumPath && step === 3,
  });
  const artistName =
    albumDetail?.album_artist ||
    (release?.artists ?? []).map((a) => a.name).join(", ").trim() ||
    "";
  const { data: artistArt, refetch: refetchArtistArt } = useQuery({
    queryKey: ["artistArtwork", artistName],
    queryFn: () => api.artistArtwork(artistName),
    enabled: !!artistName && step === 3,
    // An artist the library does not have a folder for yet is "missing", not
    // an error worth retrying three times.
    retry: false,
  });
  // Per-item outcome of the last fetch in this step (null = none yet),
  // keyed by item kind: the route answers one entry per item asked for.
  const [metaReply, setMetaReply] = useState<Partial<Record<MetadataItemKind, MetadataFetchItem>> | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);

  /** The three rows the artwork step accounts for, from the payloads the
   *  artist and album pages already read (`present` + who supplied it). The
   *  Settings toggles that gate the two artist items come from the config the
   *  wizard already holds, so a row a fetch may not touch says why. */
  const metaRows: {
    kind: MetadataItemKind;
    label: string;
    present: boolean;
    source?: string | null;
    enabled: boolean;
  }[] = [
    {
      kind: "artist_image",
      label: "Artist image",
      present: !!artistArt?.image.present,
      source: artistArt?.image.source,
      enabled: cfg?.artist_image_enabled !== false,
    },
    {
      kind: "artist_description",
      label: "Artist description",
      present: !!artistArt?.description.present,
      source: artistArt?.description.source,
      enabled: cfg?.artist_description_enabled !== false,
    },
    {
      kind: "album_description",
      label: "Album description",
      present: !!albumDetail?.artwork?.description,
      source: albumDetail?.artwork?.description_source,
      enabled: true,
    },
  ];

  /** Fetch whatever of those three is missing, for this album folder — one
   *  request per item, so the bar advances 1/3 → 3/3 and the reply says what
   *  happened to each (`fetched`, `present`, `disabled`, `not-found`,
   *  `error`). The rows are re-read afterwards, so a fetched image or
   *  description shows up as present and a click never lands on nothing. */
  const fetchArtistMeta = async () => {
    if (!albumPath) {
      toast("Open the wizard on an album folder first");
      return;
    }
    setBusy(true);
    setMetaError(null);
    const out: Partial<Record<MetadataItemKind, MetadataFetchItem>> = {};
    try {
      for (const [i, row] of metaRows.entries()) {
        setAct({
          kind: "metadata",
          label: `Metadata: ${row.label} (${i + 1}/${metaRows.length})`,
          done: i,
          total: metaRows.length,
        });
        const res = await api.albumMetadataFetch({ path: albumPath, items: [row.kind], staged });
        Object.assign(out, res.items);
      }
      setMetaReply({ ...out });
      const got = Object.entries(out).filter(([, it]) => it.state === "fetched");
      const rest = Object.entries(out).filter(([, it]) => it.state !== "fetched" && it.state !== "present");
      toast(
        got.length
          ? `Fetched ${got.map(([k]) => k.replace(/_/g, " ")).join(", ")}`
          : rest.length
            ? `Nothing fetched — ${rest.map(([k, it]) => `${k.replace(/_/g, " ")}: ${it.state}${it.detail ? ` (${it.detail})` : ""}`).join("; ")}`
            : "Everything was already present"
      );
      await Promise.all([refetchArtistArt(), refetchAlbumDetail()]);
      qc.invalidateQueries({ queryKey: ["artist"] });
    } catch (e) {
      setMetaReply({ ...out });
      setMetaError(String(e));
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  // Real dimensions of the album cover, re-read whenever a cover changes.
  const { data: coverInfo } = useQuery({
    queryKey: ["coverInfo", albumPath],
    queryFn: () => api.coverInfo(albumPath!, null, staged),
    enabled: !!albumPath && step >= 3,
  });

  const trackList = useMemo(() => {
    if (!albumPath || !lib) return [];
    for (const a of lib.artists)
      for (const al of a.albums)
        if (al.path === albumPath) return al.tracks;
    return [];
  }, [albumPath, lib]);

  // Refresh the library when the wizard opens so tags written externally
  // (e.g. Picard) since the last fetch are picked up.
  useEffect(() => {
    qc.refetchQueries({ queryKey: ["library"] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-recognize a pasted MusicBrainz release URL/ID.
  useEffect(() => {
    const id = extractMbid(mbLink);
    if (id && id !== releaseId) setReleaseId(id);
  }, [mbLink, releaseId]);

  // Detection: payload tags first, then a live on-disk scan (catches fresh
  // tags and non-standard key variants such as MUSICBRAINZ_ALBUM_ID).
  const detectReleaseId = async (): Promise<string | null> => {
    const tagged = trackList.find((t) => t.tags?.MUSICBRAINZ_ALBUMID);
    const fromPayload = tagged ? extractMbid(tagged.tags.MUSICBRAINZ_ALBUMID ?? "") : null;
    if (fromPayload) return fromPayload;
    if (!albumPath) return null;
    try {
      const r = await api.mbDetect(albumPath, staged);
      return r.mbid ?? null;
    } catch {
      return null;
    }
  };

  // The auto-search's own line in the Links step: what MusicBrainz answered
  // when the album's tags carried no MBID. It describes the visit it opened
  // on, so a new album (or a pick the user makes) drops it.
  const [autoNote, setAutoNote] = useState("");
  const autoSearched = useRef<string | null>(null);
  useEffect(() => {
    setAutoNote("");
  }, [albumPath]);

  /** The name MusicBrainz is asked about: the fetched release's title, else
   *  the album tag the files carry, else the name the import was staged under.
   *  Never the bare folder path — a library folder spells out dates, pressing
   *  countries and label, and searching for that finds nothing.
   *
   *  Empty until the app has actually read the album (a release, or the
   *  library payload's own rows): the staged name is a FALLBACK for an album
   *  whose files carry no album tag, and on a flat file drop it defaults to a
   *  track's file name — asking MusicBrainz about that finds nothing and
   *  spends the one search the automatic run gets. */
  const autoSearchName = (): string => {
    if (!release && !trackList.length) return "";
    const first = stepTracks[0]?.path ?? trackList[0]?.path;
    return (
      release?.title ||
      (first ? findTrack(first)?.tags?.ALBUM : "") ||
      uploaded[albumIndex]?.name ||
      ""
    ).trim();
  };

  /** Ask MusicBrainz for the release this album's own tags say it is.
   *
   *  The wizard's own fetch for an album with no MBID in its files: the tags
   *  (or the name the import was staged under) still name the artist and the
   *  album, so they are the query. A hit whose title AND artist match what the
   *  files carry is fetched into the step, exactly as a tag-detected ID is;
   *  anything less exact is left as the step's own search results for the user
   *  to pick — a partial name never links a release. False = nothing was
   *  prefilled (and, with no name to ask about, nothing was asked). */
  const searchReleaseFromTags = async (): Promise<boolean> => {
    const first = stepTracks[0]?.path;
    const artist = (
      (first && trackArtist(first)) ||
      (release?.artists ?? []).map((a) => a.name).join(", ") ||
      ""
    ).trim();
    const album = autoSearchName();
    if (!album) return false;
    setFetchStatus("Searching MusicBrainz for this album…");
    try {
      const query = artist
        ? `release:"${album}" AND artist:"${artist}"`
        : `release:"${album}"`;
      const hits = await api.mbSearchReleases(query, "release");
      const list = (Array.isArray(hits) ? hits : []).filter(isSearchHit);
      const want = matchKey(album);
      const who = matchKey(artist);
      const exact = list.find((h) => matchKey(h.title ?? "") === want && (!who || matchKey(h.artist ?? "").includes(who)));
      if (exact) {
        setAutoNote(
          `No MusicBrainz ID in the tags — MusicBrainz answers “${exact.title}”` +
          `${exact.artist ? ` by ${exact.artist}` : ""}${exact.date ? ` (${exact.date.slice(0, 4)})` : ""} ` +
          `for this album. Review it, then Continue — or pick another release below.`
        );
        setMbLink(`https://musicbrainz.org/release/${exact.id}`);
        setReleaseId(exact.id);
        pickRelease(exact.id);
        return true;
      }
      setSearchMode("release");
      setMbSearch(query);
      setSearchHits(list);
      setAutoNote(
        list.length
          ? `No MusicBrainz ID in the tags — ${list.length} release(s) on MusicBrainz match this album below; pick the right one.`
          : `No MusicBrainz ID in the tags and MusicBrainz knows no release named “${album}”${artist ? ` by ${artist}` : ""} — paste a link, or search by track or catalog number.`
      );
      return false;
    } catch (e) {
      setAutoNote(`MusicBrainz search failed — ${e}`);
      return false;
    } finally {
      setFetchStatus(null);
    }
  };

  // Auto-detect a Picard-tagged MusicBrainz release and fetch it
  // automatically when the Links step opens.
  //
  // The guard is the album path plus whether the attempt FINISHED, not a bare
  // "ran once" flag: this effect's dependencies include the library payload,
  // which the mount below refetches, so it re-runs mid-flight on a fresh load.
  // The flag used to be set before the work, and the cancelled run's answer
  // was dropped while the re-run returned early — leaving the step on
  // "Scanning track tags…" with an empty release field forever, so the fetch
  // never ran and the button had to be pressed by hand on every album.
  const autoDetected = useRef<{ path: string | null; done: boolean }>({ path: null, done: false });
  const [detectStatus, setDetectStatus] = useState<"idle" | "scanning" | "found" | "none">("idle");
  useEffect(() => {
    if (step !== 1 || !albumPath || releaseId) return;
    const attempt = autoDetected.current;
    if (attempt.path === albumPath && attempt.done) return;
    attempt.path = albumPath;
    attempt.done = false;
    let cancelled = false;
    setDetectStatus("scanning");
    (async () => {
      const id = await detectReleaseId();
      // A newer run owns the step's state now; this one leaves `done` false so
      // that run — or the next one — does the fetch.
      if (cancelled) return;
      if (!id) {
        setDetectStatus("none");
        // No MBID in the tags either: the fetch the step offers runs here.
        // Leaving step 0 ("Import … into library") IS the continue that used
        // to be followed by remembering to press "Fetch release & auto-match"
        // on every album, so the wizard asks MusicBrainz for the release the
        // album's own tags name. Once per album (`autoSearched`).
        //
        // With no name to ask about yet, this is NOT the end of the attempt:
        // the album's tags arrive with the library payload, which is why
        // `done` stays false here and this effect runs again when they land.
        const name = autoSearchName();
        if (!name) return;
        attempt.done = true;
        if (autoSearched.current !== albumPath) {
          autoSearched.current = albumPath;
          searchReleaseFromTags();
        }
        return;
      }
      attempt.done = true;
      setDetectStatus("found");
      setDetectedFromTags(true);
      setMbLink(`https://musicbrainz.org/release/${id}`);
      setReleaseId(id);
      pickRelease(id);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, albumPath, trackList, releaseId]);

  // Manual "Detect from tags" — always available, one click.
  const detectFromTags = async () => {
    if (!albumPath) {
      toast("No album selected");
      return;
    }
    setDetectStatus("scanning");
    try {
      const id = await detectReleaseId();
      if (!id) {
        setDetectStatus("none");
        toast("No MusicBrainz release ID found in the track tags");
        return;
      }
      setDetectStatus("found");
      setDetectedFromTags(true);
      setMbLink(`https://musicbrainz.org/release/${id}`);
      setReleaseId(id);
      pickRelease(id);
    } catch (e) {
      setDetectStatus("none");
      toast.error(String(e));
    }
  };

  // Fetch button: falls back to tag detection when the field is empty, and to
  // the MusicBrainz search when the tags carry nothing either — a press that
  // can do nothing at all would be the dead button this step already fixed
  // once. Every press asks again: only the automatic run is once per album.
  const handleFetch = async () => {
    let id = releaseId || extractMbid(mbLink) || "";
    if (!id && albumPath) {
      setDetectStatus("scanning");
      const detected = await detectReleaseId();
      if (detected) {
        setDetectStatus("found");
        setDetectedFromTags(true);
        setMbLink(`https://musicbrainz.org/release/${detected}`);
        id = detected;
      } else {
        setDetectStatus("none");
      }
    }
    if (!id) {
      if (albumPath) await searchReleaseFromTags();
      return;
    }
    pickRelease(id);
  };

  // Debounced RYM link validation. The server reports WHICH page it is, so an
  // artist paste is routed to the artist field instead of being written as the
  // album link, and a song page is refused with the reason.
  useEffect(() => {
    if (!rymLink.trim()) {
      setRymValid(null);
      setRymKind(null);
      return;
    }
    const t = setTimeout(async () => {
      try {
        const r = (await api.rymValidate(rymLink.trim())) as { valid: boolean; kind?: string | null };
        if (r.kind === "artist") {
          // Not a wrong paste, just the wrong field — move it, say so, and
          // leave the album field empty so auto-find can fill it.
          setRymArtistLink(rymLink.trim());
          setRymLink("");
          setRymValid(null);
          setRymKind(null);
          setRymNote("That is a RateYourMusic artist page — moved to the artist link");
          return;
        }
        // `valid` from the server just means "a RYM URL"; for the ALBUM field
        // only an album page counts, so a song/other page reads as invalid
        // here (the chip and the note say which).
        setRymValid(r.valid && (r.kind == null || r.kind === "album"));
        setRymKind(r.kind ?? null);
        setRymNote(
          r.kind === "song"
            ? "That is a RateYourMusic song page, not an album"
            : r.kind === "other"
              ? "That is a RateYourMusic page, but not an album"
              : ""
        );
      } catch {
        setRymValid(false);
        setRymKind(null);
        setRymNote("Could not check the link");
      }
    }, 400);
    return () => clearTimeout(t);
  }, [rymLink]);

  // The artist field validates through the same call, and accepts only an
  // artist page: a song or album paste here would be written to every track as
  // RATEYOURMUSIC_ARTIST and never resolve to the artist.
  useEffect(() => {
    if (!rymArtistLink.trim()) {
      setRymArtistValid(null);
      setRymArtistKind(null);
      setRymArtistNote("");
      return;
    }
    const t = setTimeout(async () => {
      try {
        const r = (await api.rymValidate(rymArtistLink.trim())) as { valid: boolean; kind?: string | null };
        const kind = r.kind ?? null;
        setRymArtistValid(r.valid && (kind == null || kind === "artist"));
        setRymArtistKind(kind);
        setRymArtistNote(
          kind === "artist" || kind == null
            ? ""
            : kind === "album"
              ? "That is a RateYourMusic album page — paste the artist page"
              : kind === "song"
                ? "That is a RateYourMusic song page, not an artist"
                : "That is a RateYourMusic page, but not an artist"
        );
      } catch {
        setRymArtistValid(false);
        setRymArtistKind(null);
        setRymArtistNote("Could not check the link");
      }
    }, 400);
    return () => clearTimeout(t);
  }, [rymArtistLink]);

  const defaultTrackName = (p: string) => {
    const base = p.split("/").pop() ?? "";
    return base.replace(/^\d+\s*[-._]\s*/, "").replace(/\.[^.]+$/, "").trim();
  };

  const currentAlbumName = uploaded[albumIndex]?.name ?? albumPath?.split("/").pop() ?? "";

  // Tracks for the per-track steps (genres/lyrics/advisory). Prefer the
  // library payload, but fall back to the matched suggestions — the library
  // only contains the exact album path, while matching scans the folder
  // directly (nested/multi-album structures).
  // Third fallback: direct folder scan — always reflects what's on disk.
  const [scannedTracks, setScannedTracks] = useState<Track[]>([]);

  /** One row of /api/album/scan-tracks as a Track. The scan reports the
   *  track's REAL lyrics state (`lyrics_embedded`/`lyrics_lrc`, computed by
   *  the grader's own detection) — hardcoding "no lyrics" here made every
   *  track of a staged album claim lyrics it already carried. */
  const scanRow = (t: ScanTrackRow): Track => ({
    path: t.path,
    file: t.file,
    tracknumber: t.tracknumber ?? null,
    discnumber: t.discnumber ?? null,
    issues: [],
    values: {},
    audit: null,
    log_grade: null,
    lyrics_embedded: !!t.lyrics_embedded,
    lyrics_lrc: !!t.lyrics_lrc,
    unreadable: false,
    tech: t.tech ?? {},
    tags: t.tags ?? {},
    grade_pass: false,
    lyrics_present: !!t.lyrics_present,
  });

  /** Re-read the folder a step works on — the wizard's own source of truth
   *  for an album the library tree does not list (a staged import). Called
   *  after a fetch so the step shows what just landed instead of the state
   *  it had before. */
  const rescanTracks = async (): Promise<Track[]> => {
    if (!albumPath) return [];
    const rows = (await api.scanTracks(albumPath, staged)).tracks.map(scanRow);
    setScannedTracks(rows);
    return rows;
  };

  useEffect(() => {
    setScannedTracks([]);
    // Library rows already carry the real lyrics state; everything else (a
    // matched-but-unlinked or staged album) needs the scan for it.
    if (trackList.length || !albumPath) return;
    let cancelled = false;
    api.scanTracks(albumPath, staged)
      .then((r) => {
        if (!cancelled) setScannedTracks(r.tracks.map(scanRow));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [albumPath, trackList.length]);

  const stepTracks: Track[] = useMemo(() => {
    const base: Track[] = trackList.length
      ? trackList
      : suggestions.length
        ? suggestions.map((s) => ({
            path: s.local,
            file: s.file,
            issues: [],
            values: {},
            audit: null,
            log_grade: null,
            lyrics_embedded: false,
            lyrics_lrc: false,
            unreadable: false,
            tech: {},
            tags: {},
            grade_pass: false,
            lyrics_present: false,
          }))
        : scannedTracks;
    // Suggestion rows carry no lyrics of their own: the folder scan knows
    // what these files actually hold, so it fills the state in.
    if (!scannedTracks.length || base === scannedTracks) return base;
    const onDisk = new Map(scannedTracks.map((t) => [t.path, t]));
    return base.map((t) => {
      const s = onDisk.get(t.path);
      return s
        ? {
            ...t,
            lyrics_embedded: s.lyrics_embedded,
            lyrics_lrc: s.lyrics_lrc,
            lyrics_present: s.lyrics_present,
          }
        : t;
    });
  }, [trackList, suggestions, scannedTracks]);

  // ---- per-disc grouping helpers (shared by Match and Genres steps) ----
  const suggByPath = useMemo(() => new Map(suggestions.map((s) => [s.local, s])), [suggestions]);

  const discOfTrack = (t: { path: string; tags?: { DISCNUMBER?: string | null } }): number | null => {
    const m = suggByPath.get(t.path)?.release_track;
    if (m) return m.disc;
    return parseDisc(t.tags?.DISCNUMBER);
  };

  const discOfSuggestion = (s: MatchSuggestion): number | null => {
    if (s.release_track) return s.release_track.disc;
    const t = trackList.find((x) => x.path === s.local);
    return parseDisc(t?.tags?.DISCNUMBER);
  };

  const toggleDisc = (d: number | null) =>
    setCollapsedDiscs((s) => {
      const next = new Set(s);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });

  // ---------------- Step 0: selection + separation ----------------
  const adoptImports = (list: ImportFile[], fallback: string) => {
    if (list.length && fallback && !albumName) setAlbumName(fallback);
    setAlbums(detectAlbums(list, fallback || albumName || "New Album"));
    // A fresh selection starts with nothing excluded: the tick state is keyed
    // by relPath, so a second drop of a same-named folder would otherwise
    // silently carry the previous album's exclusions into it.
    setExcluded(new Set());
  };

  const handleFiles = (list: FileList | File[]) => {
    const arr: ImportFile[] = [];
    for (const f of Array.from(list)) {
      const rel = (f as any).webkitRelativePath
        ? (f as any).webkitRelativePath.replace(/\\/g, "/")
        : f.name.replace(/\\/g, "/");
      if (ALLOWED.test(rel)) arr.push({ file: f, relPath: rel });
    }
    adoptImports(arr, arr[0]?.relPath.split("/")[0] ?? "");
  };

  const walkEntry = async (entry: any, prefix: string, out: ImportFile[]) => {
    if (entry.isFile) {
      const f = await new Promise<File>((resolve, reject) => entry.file(resolve, reject));
      out.push({ file: f, relPath: prefix ? `${prefix}/${f.name}` : f.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const entries: any[] = await new Promise((resolve, reject) => {
        const all: any[] = [];
        const readBatch = () =>
          reader.readEntries((batch: any[]) => {
            if (!batch.length) return resolve(all);
            all.push(...batch);
            readBatch();
          }, reject);
        readBatch();
      });
      for (const e of entries) await walkEntry(e, prefix ? `${prefix}/${entry.name}` : entry.name, out);
    }
  };

  const handleDrop = async (items: DataTransferItemList) => {
    const out: ImportFile[] = [];
    const roots: any[] = [];
    for (const item of Array.from(items)) {
      const entry = item.webkitGetAsEntry?.();
      if (entry) roots.push(entry);
    }
    if (roots.some((r) => r.isDirectory)) {
      for (const root of roots) {
        await walkEntry(root, root.isDirectory ? root.name : "", out);
      }
      adoptImports(out.filter((o) => ALLOWED.test(o.relPath)), roots.find((r) => r.isDirectory)?.name ?? "");
    } else {
      const files = roots.filter((r) => r.isFile).map((r) => r.file) as File[];
      handleFiles(files);
    }
  };

  const pickFolderBrowser = async () => {
    try {
      // Preferred: File System Access API (Chromium/Edge).
      const picker = (window as any).showDirectoryPicker;
      if (picker) {
        const dir = await picker({ mode: "read" });
        const out: ImportFile[] = [];
        const walk = async (entry: any, prefix: string) => {
          for await (const e of entry.values()) {
            if (e.kind === "file") {
              const f = await e.getFile();
              out.push({ file: f, relPath: prefix ? `${prefix}/${f.name}` : f.name });
            } else if (e.kind === "directory") {
              await walk(e, prefix ? `${prefix}/${e.name}` : e.name);
            }
          }
        };
        await walk(dir, "");
        adoptImports(out.filter((o) => ALLOWED.test(o.relPath)), dir.name || "");
        return;
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") toast.error(String(e));
      return;
    }
    // Fallback: webkitdirectory input (Chrome/Edge/Firefox/WebView2).
    document.getElementById("import-folder")?.click();
  };

  const pickFolderNative = async () => {
    const inTauri = !!(window as any).__TAURI_INTERNALS__;
    if (IN_MOBILE_SHELL) {
      // The mobile shell registers no commands: there is no native folder
      // dialog to open, and the phone cannot read the server's filesystem
      // anyway. Say so, then fall through to the file input, which is the
      // one picker a phone actually has.
      toast("Folder browsing is a desktop feature — on a phone, import from the web UI or the desktop app.");
      document.getElementById("import-folder")?.click();
      return;
    }
    if (!inTauri) {
      // no native dialog in a plain browser — fall back to the folder input
      document.getElementById("import-folder")?.click();
      return;
    }
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const picked = await invoke<string | null>("pick_folder");
      if (!picked) return;
      const scan = await api.importScan(picked);
      setSource("native");
      setNativeRoot(picked);
      const list: ImportFile[] = scan.files
        .filter((f) => ALLOWED.test(f.relPath))
        .map((f) => ({ file: null, relPath: f.relPath }));
      adoptImports(list, picked.split(/[\\/]/).pop() ?? "");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const renameGroup = (i: number, name: string) =>
    setAlbums((gs) => gs.map((g, j) => (j === i ? { ...g, name } : g)));

  const moveFile = (from: number, to: number, f: ImportFile) => {
    if (from === to) return;
    setAlbums((gs) =>
      gs.map((g, i) => {
        if (i === from) return { ...g, files: g.files.filter((x) => x !== f) };
        if (i === to) return { ...g, files: [...g.files, f] };
        return g;
      })
    );
  };

  const addAlbum = () =>
    setAlbums((gs) => [...gs, { name: `Album ${gs.length + 1}`, root: "", files: [] }]);

  const removeAlbum = (i: number) => {
    setAlbums((gs) => {
      if (gs.length <= 1) return gs;
      const removed = gs[i];
      const rest = gs.filter((_, j) => j !== i);
      if (removed.files.length) {
        rest[0] = { ...rest[0], files: [...removed.files, ...rest[0].files] };
      }
      return rest;
    });
  };

  const doImport = async () => {
    // Excluded files are dropped here, before anything is uploaded or moved:
    // that is what makes a PARTIAL album import (one track of twelve) work.
    // The native/folder import MOVES the source directory, so there is no
    // subset to move — exclusion only exists on the upload path.
    const skip = source === "web" ? excluded : new Set<string>();
    const included = (g: AlbumGroup) => g.files.filter((f) => !skip.has(f.relPath));
    const groups = albums.filter((g) => g.name.trim() && included(g).length);
    if (!groups.length) {
      toast("Nothing to import — add files first");
      return;
    }
    setUploading(true);
    try {
      const results: { name: string; path: string }[] = [];
      const failed: { name: string; error: unknown }[] = [];
      for (const g of groups) {
        const name = g.name.trim();
        const keep = included(g);
        if (source === "web") {
          const filesToSend = keep.filter((f) => f.file) as { file: File; relPath: string }[];
          if (!filesToSend.length) continue;
          try {
            const res = await api.importUpload(name, filesToSend);
            results.push({ name, path: res.album_path });
          } catch (e) {
            failed.push({ name, error: e });
          }
        } else {
          const src = nativeRoot ? (g.root ? `${nativeRoot}/${g.root}` : nativeRoot) : "";
          if (!src) continue;
          try {
            const res = await api.importIngest(src, name);
            results.push({ name, path: res.path });
          } catch (e) {
            failed.push({ name, error: e });
          }
        }
      }
      if (!results.length) {
        toast(failed.length ? String(failed[0].error) : "Nothing to import");
        return;
      }
      setUploaded(results);
      setAlbumIndex(0);
      setAlbumPath(results[0].path);
      setStep(1);
      qc.invalidateQueries({ queryKey: ["library"] });
      // Committed. With metadata_review on (Settings → Metadata) the review is
      // offered right here, while the album is fresh, instead of on a later
      // visit to its page.
      if (cfg?.metadata_review === true) setReviewPath(results[0].path);
      // Several albums: hand the staged queue to the bulk job, which moves
      // whatever is still outside the library and runs the import chain per
      // album. The queue panel polls api.importBulkStatus for progress.
      if (results.length > 1) {
        try {
          const job = await api.importBulk(results.map((r) => ({ path: r.path })));
          if (job.ok && job.job) setBulkJob(job.job);
          else toast(`Queue import: ${job.error ?? "could not start"}`);
        } catch (e) {
          toast(`Queue import: ${e}`);
        }
      }
      toast(
        failed.length
          ? `Imported ${results.length} album${results.length > 1 ? "s" : ""} — failed: ${failed.map((f) => f.name).join(", ")}`
          : `Imported ${results.length} album${results.length > 1 ? "s" : ""}`
      );
    } catch (e) {
      toast.error(String(e));
    } finally {
      setUploading(false);
    }
  };

  // ---------------- Step 1: links ----------------
  const [searchMode, setSearchMode] = useState<"release" | "track" | "catno" | "barcode">("release");
  const doSearch = async (q?: string) => {
    const query = q ?? mbSearch;
    if (!query.trim()) return;
    setBusy(true);
    // A hand search replaces what the automatic one said, so its note goes.
    setAutoNote("");
    try {
      setSearchHits(await api.mbSearchReleases(query.trim(), searchMode));
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const [artistQuery, setArtistQuery] = useState("");
  const [artistHits, setArtistHits] = useState<{ id: string; name: string; type?: string }[]>([]);
  const doArtistSearch = async () => {
    if (!artistQuery.trim()) return;
    setBusy(true);
    try {
      const hits = await api.mbSearchArtists(artistQuery.trim());
      setArtistHits(Array.isArray(hits) ? hits : []);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };
  const applyArtist = (name: string) => {
    setArtistHits([]);
    setArtistQuery("");
    setMbSearch(`artist:"${name}"`);
    setSearchMode("release");
    doSearch(`artist:"${name}"`);
  };

  const pickRelease = async (id: string, target: string | null = albumPath) => {
    if (!id) return;
    setBusy(true);
    setFetchStatus("Fetching release from MusicBrainz…");
    try {
      // MusicBrainz rate-limits and blips — retry the remote calls.
      const rel = await withRetry(() => api.mbRelease(id));
      setRelease(rel);
      setReleaseId(id);
      setFetchStatus("Matching local tracks to the release…");
      const matched = await api.mbMatch(target!, id, staged);
      setSuggestions(matched.suggestions);
      if (!matched.suggestions.length) {
        toast("No audio tracks found in this folder — check the album folder contains the music files");
      } else {
        toast(`Matched ${matched.suggestions.filter((s) => s.matched).length}/${matched.suggestions.length} tracks`);
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      setFetchStatus(null);
    }
  };

  /** ONE genre source, written straight to the files.
   *
   *  The wizard has exactly two of these — "Genres from MusicBrainz" and
   *  "Genres from RateYourMusic" — each asking that source alone, so a
   *  blocked or empty one is never hidden behind the other's answer. The
   *  server reports what the source wrote (`updated`, the names, and its own
   *  reason for staying silent) and the step re-reads the files, so what
   *  Continue would save is what actually landed. */
  const importGenresFrom = async (source: "musicbrainz" | "rateyourmusic") => {
    const targets = albumTargets();
    if (!targets.length) {
      toast("Nothing to import genres for yet");
      return;
    }
    const label = source === "musicbrainz" ? "MusicBrainz" : "RateYourMusic";
    setBusy(true);
    setGenreError(null);
    setGenreJobResult(null);
    setAct({ label: `Importing genres from ${label}…` });
    try {
      const res = await api.genresImport(targets, genreLimit ?? undefined, [source], staged);
      setGenreJobResult(res);
      const perSource = Object.entries(res.per_source ?? {})
        .filter(([, names]) => names.length)
        .map(([name, names]) => `${name} ${names.length}`)
        .join(", ");
      // The source wrote the files: re-read them so the step shows what
      // landed instead of the state it had before.
      if (albumPath && stepTracks.length) {
        try {
          const rows = await rescanTracks();
          const onDisk = new Map(rows.map((r) => [r.path, splitGenres(r.tags?.GENRE)]));
          setGenres((g) => {
            const next = { ...g };
            for (const t of stepTracks) if (onDisk.has(t.path)) next[t.path] = onDisk.get(t.path)!;
            return next;
          });
        } catch {
          /* the step keeps what it had; the report below still says what ran */
        }
      }
      qc.invalidateQueries({ queryKey: ["library"] });
      const note = res.notes?.[source];
      toast(
        res.updated
          ? `${label}: ${res.updated} track(s) updated${perSource ? ` — ${perSource}` : ""}${note ? ` (${note})` : ""}`
          : `${label} had no genres to write${note ? ` — ${note}` : ""}`
      );
    } catch (e) {
      setGenreError(String(e));
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  /** Album paths the wizard's own actions act on: the staged batch when files
   *  were just imported, else the album the wizard was opened on (?album=). */
  const albumTargets = (): string[] =>
    uploaded.length ? uploaded.map((a) => a.path) : albumPath ? [albumPath] : [];

  /** Ask RYM for this album's and this artist's pages and PREFILL both fields.
   *  Nothing is written here: an empty field is filled for review, and a field
   *  the user already typed in is left alone. False = nothing to look up yet. */
  const findRymLinks = async (): Promise<boolean> => {
    // Who and what RYM is asked about: the fetched release first, else the
    // album's own tags — the only source an auto-imported album has.
    const artist =
      (release?.artists ?? []).map((a) => a.name).join(", ").trim() ||
      (stepTracks.length ? trackArtist(stepTracks[0].path) : "");
    const album = trackAlbum ?? currentAlbumName;
    if (!artist && !album) return false;
    setFindingLinks(true);
    try {
      const r = await api.rymResolve(artist, album);
      const foundAlbum = r.album;
      const foundArtist = r.artist;
      if (foundAlbum) setRymLink((cur) => (cur.trim() ? cur : foundAlbum));
      if (foundArtist) setRymArtistLink((cur) => (cur.trim() ? cur : foundArtist));
      setRymNote(
        foundAlbum || foundArtist
          ? "found on RateYourMusic — review, then Continue saves it"
          : r.note || "nothing found on RateYourMusic — paste the URLs"
      );
    } catch {
      setRymNote("Lookup failed — paste the URLs instead");
    } finally {
      setFindingLinks(false);
    }
    return true;
  };

  // Once per album on entering the Links step: an auto-imported album lands
  // here with no links at all, so the lookup waits for the tags/release that
  // give it a name, then runs itself.
  const linksAutoFound = useRef<string | null>(null);
  useEffect(() => {
    if (step !== 1 || !albumPath || linksAutoFound.current === albumPath) return;
    findRymLinks().then((ran) => {
      if (ran) linksAutoFound.current = albumPath;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, albumPath, stepTracks, release]);

  const nextFromLinks = async () => {
    const rid = releaseId || extractMbid(mbLink) || "";
    if (!albumPath || !rid) {
      toast("Enter a valid MusicBrainz release URL or ID first");
      return;
    }
    setBusy(true);
    setAct({ label: "Saving links…" });
    try {
      // Only an ALBUM page may be stored as the album link — rymValid is
      // already false for a song/other page, and an artist paste never lands
      // in this field at all.
      const albumLink = rymValid ? rymLink.trim() : undefined;
      await api.importCommit(albumPath, mbLink || `https://musicbrainz.org/release/${rid}`, albumLink, staged);
      // The artist page is artist-level, so it goes on every track as
      // RATEYOURMUSIC_ARTIST — the same tag the artist page's editor writes.
      // Only a link the server confirmed as an ARTIST page is stored: a song
      // or album paste in this field would be a wrong artist link forever.
      const artistLink = rymArtistValid ? rymArtistLink.trim() : "";
      if (artistLink) {
        if (!stepTracks.length) {
          toast("Artist link needs the album's tracks — finish matching first");
        } else {
          const writes: Record<string, Record<string, string>> = {};
          for (const t of stepTracks) (writes[t.path] ??= {}).RATEYOURMUSIC_ARTIST = artistLink;
          await api.mbAssign(writes, staged);
        }
      }
      toast("Links saved to album");
      setStep(2);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  // ---------------- AcoustID: fingerprint the staged audio ----------------
  /** Stage 2 of the AcoustID step: what the audio actually is. Says so and
   *  leaves the manual MusicBrainz search alone when it cannot run. */
  const runAcoustid = async () => {
    // Everything staged, else the single album the wizard is on (?album=).
    const paths = uploaded.length ? uploaded.map((a) => a.path) : albumPath ? [albumPath] : [];
    if (!paths.length) {
      toast("Import the files first — AcoustID fingerprints the staged album");
      return;
    }
    setAcoustidBusy(true);
    setAct({ label: `Fingerprinting ${paths.length} album(s) with AcoustID…` });
    try {
      const res = await api.importAcoustid(paths, false, staged);
      setAcoustid(res);
      // A fresh run's rows carry no accepted match yet, so no row may offer
      // the submission the ids alone make possible.
      setAcoustidApplied({});
      if (!res.available) toast(`Fingerprinting unavailable — ${res.note}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setAcoustidBusy(false);
    }
  };

  /** Which edition of a matched release group to use: the release with the
   *  track count AcoustID heard, else the group's earliest. */
  const releaseForRow = async (row: AcoustidAlbumMatch): Promise<string | null> => {
    if (!row.release_group_id) return null;
    const group: { releases?: { id?: string; track_count?: number }[] } = await withRetry(() =>
      api.mbReleaseGroup(row.release_group_id!)
    );
    const releases = group?.releases ?? [];
    const pick = releases.find((r) => r.track_count === row.total) ?? releases[0];
    return pick?.id ?? null;
  };

  /** "Use this release" — resolves the matched release group to a release and
   *  hands it to the SAME fetch + auto-match flow the wizard already uses
   *  (api.mbReleaseGroup → api.mbMatch), so tags are written by one writer.
   *  Accepting the match also writes its AcoustID identity into the files
   *  (apply=true: ACOUSTID_ID + ACOUSTID_FINGERPRINT). In queue mode the
   *  wizard follows the album the release is for. */
  const useAcoustidRelease = async (row: AcoustidAlbumMatch) => {
    const index = uploaded.findIndex((a) => a.path === row.path);
    if (index >= 0 && index !== albumIndex) switchAlbum(index);
    setAcoustidBusy(true);
    setAct({ label: "Resolving the AcoustID match on MusicBrainz…" });
    setFetchStatus("Resolving the release group on MusicBrainz…");
    try {
      const rid = await releaseForRow(row);
      if (!rid) {
        toast("That release group has no releases on MusicBrainz — search manually below");
        return;
      }
      setDetectedFromTags(false);
      setMbLink(`https://musicbrainz.org/release/${rid}`);
      setReleaseId(rid);
      await pickRelease(rid, row.path);
      // Accepting the match: keep the fingerprint on the album, so the
      // on-disk check agrees with what was just matched. The row the wizard
      // was SHOWN goes with it: the server writes straight from that payload
      // (no fpcalc, no lookup — a transient network failure on this second
      // pass used to turn a displayed match into zero tags).
      try {
        const applied = await api.importAcoustid([row.path], true, staged, row);
        const appliedRow = applied.albums?.find((a) => a.path === row.path);
        const tagged = appliedRow?.tagged ?? 0;
        // The failed writes are the answer that matters when nothing (or not
        // everything) went in. "0 tagged" alone used to read as "the files
        // carry none of the tag families it targets" — a .wv album is a real
        // match no writer can touch, and the server names it per file.
        const problems = (appliedRow?.writes ?? []).filter((w) => !w.ok);
        if (problems.length) {
          const total = (appliedRow?.writes ?? []).length || problems.length;
          toast.error(
            `AcoustID identity tags written to ${tagged} of ${total} track(s) — ${writeProblems(problems)}`
          );
        } else {
          toast(`Identity tags written to ${tagged} track(s)`);
        }
        if (tagged) setAcoustidApplied((m) => ({ ...m, [row.path]: true }));
      } catch (e) {
        toast(`Matched, but the AcoustID identity tags failed: ${e}`);
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setAcoustidBusy(false);
      setFetchStatus(null);
    }
  };

  /** "Submit to AcoustID" — publish the fingerprint/id pair the ACCEPTED match
   *  wrote into the files to AcoustID's public database. Nothing is
   *  fingerprinted again and nothing is written locally (mlo.acoustid.
   *  submit_fingerprints reads the pair back off the files), and the reply is
   *  the service's own: how many it took, or the sentence it refused with
   *  (no `acoustid_user_key`, or the key refused) — never a generic "failed".
   *  The block only offers this for a row whose ids are on the files, which is
   *  what makes the press mean something. */
  const submitAcoustidRelease = async (
    row: AcoustidAlbumMatch
  ): Promise<AcoustidSubmitReply> => {
    setAcoustidBusy(true);
    setAct({ label: "Publishing these fingerprints to AcoustID…" });
    try {
      const result = await api.importAcoustidSubmit([row.path], staged);
      if (!result.available) {
        toast.error(`AcoustID refused the submission — ${result.note}`);
      } else if (result.submitted) {
        toast.success(
          `AcoustID accepted ${result.submitted} of ${result.tracks.total} fingerprint(s)` +
            (result.tracks.skipped ? ` · ${result.tracks.skipped} skipped` : "")
        );
      } else {
        toast.error(
          `AcoustID took no fingerprint — ${result.note || `${result.tracks.skipped} track(s) had nothing to submit`}`
        );
      }
      return { ok: true, result };
    } catch (e) {
      // The sentence the route refused with, verbatim: 409 while manual
      // importing is off, 400 without the confirm the client always sends.
      toast.error(String(e));
      return { ok: false, error: String(e) };
    } finally {
      setAct(null);
      setAcoustidBusy(false);
    }
  };

  /** Queue mode "match to release": the release chosen above is applied to
   *  EVERY staged album — one release across the queue (a box set's discs, a
   *  rip split over several folders). Per-album failures are reported, never
   *  fatal: the albums are already in the library. */
  const matchQueueToRelease = async () => {
    const rid = releaseId || extractMbid(mbLink) || "";
    if (!uploaded.length || !rid) {
      toast("Pick a MusicBrainz release first (Links step, or AcoustID above)");
      return;
    }
    setMatchAllBusy(true);
    setAct({ label: `Matching ${uploaded.length} album(s) to the release…` });
    const failed: string[] = [];
    let matched = 0;
    try {
      for (const a of uploaded) {
        setFetchStatus(`Matching ${a.name} to the release…`);
        try {
          const res = await api.mbMatch(a.path, rid, staged);
          if (!res.suggestions.length) {
            failed.push(`${a.name}: no audio files`);
            continue;
          }
          await assignTracks(a.path, res.release, res.suggestions);
          matched += 1;
        } catch (e) {
          failed.push(`${a.name}: ${e}`);
        }
      }
      toast(
        failed.length
          ? `Matched ${matched}/${uploaded.length} album(s) — ${failed.slice(0, 3).join("; ")}`
          : `Matched and tagged ${matched} album(s) to the release`
      );
      qc.invalidateQueries({ queryKey: ["library"] });
    } finally {
      setAct(null);
      setMatchAllBusy(false);
      setFetchStatus(null);
    }
  };

  // ---------------- Step 2: matching ----------------
  const setSuggestion = (path: string, disc: number, position: number) => {
    const m = release?.media.find((x) => x.disc === disc && x.position === position);
    setSuggestions((ss) => ss.map((s) => (s.local === path ? { ...s, matched: !!m, confidence: 1, release_track: m ?? null } : s)));
  };

  /** Write ONE album's matched rows to its files. The wizard's only tag
   *  writer: the single-album Confirm calls it for the album on screen, the
   *  queue's "match to release" calls it once per queued album. */
  const assignTracks = async (path: string, release: MBRelease, suggestions: MatchSuggestion[]) => {
    const writes: Record<string, Record<string, string | null>> = {};
    const albumArtist = (release?.artists ?? []).map((a) => a.name).join(", ") || null;
    const mediumFormat = release?.medium_formats?.[0] || mediaType || null;
    for (const s of suggestions) {
      const t = s.release_track;
      writes[s.local] = {
        // links / IDs
        MUSICBRAINZ_TRACKID: t?.recording_mbid ?? null,
        MUSICBRAINZ_ARTISTID: t?.artist_mbids?.[0] ?? null,
        MUSICBRAINZ_ALBUMID: release?.id ?? null,
        MUSICBRAINZ_RELEASEGROUPID: release?.release_group_id ?? null,
        MUSICBRAINZ_RELEASEID: release?.id ?? null,
        MUSICBRAINZ_ALBUMARTISTID: release?.artists?.[0]?.mbid ?? null,
        // metadata
        TITLE: t?.title ?? null,
        ARTIST: t?.artist_credit || (release?.artists ?? [])[0]?.name || null,
        ALBUM: release?.title ?? null,
        ALBUMARTIST: albumArtist,
        DATE: release?.date || null,
        // UNPADDED: a file tag stores "1", never "01". Zero-padding is a
        // FILENAME/display convention (the naming script's pattern), not a
        // metadata one — MusicBrainz, Picard, beets and every player write
        // the bare integer, and a padded tag sorts and diffs wrong against
        // them.
        TRACKNUMBER: t ? String(t.position) : null,
        TRACKTOTAL: release?.media?.length ? String(release.media.length) : null,
        DISCNUMBER: t ? String(t.disc) : null,
        DISCTOTAL: release?.medium_count ? String(release.medium_count) : null,
        MEDIA: mediumFormat,
        RELEASETYPE: release?.release_type || null,
        // The release's WHOLE event set, "; "-joined — the one spelling the
        // backend writes and the badges read back (`releaseCountries`). `country`
        // alone is MusicBrainz's FIRST event: writing it claimed a release that
        // came out in several countries came out in one of them.
        RELEASECOUNTRY:
          (release?.countries ?? []).map((c) => c.code).filter(Boolean).join("; ")
          || release?.country || null,
        CATALOGNUMBER: release?.catalog_number || null,
        LABEL: release?.label || null,
      };
    }
    await api.mbAssign(writes, staged);
    // Record the RELEASE's own tracklist on the folder. A partial import
    // (some of these tracks never brought in) leaves no other trace of what
    // is absent, so the album page diffs against this list and greys out
    // the missing rows. A failure here must not lose the tag writes that
    // just succeeded, so it is reported and the caller moves on.
    try {
      await api.importExpected(
        path,
        release.id,
        (release.media ?? []).map((m) => ({
          disc: m.disc,
          position: m.position,
          title: m.title,
          recording_mbid: m.recording_mbid ?? null,
        })),
        staged
      );
    } catch (e) {
      toast(`Metadata saved, but the release tracklist could not be recorded: ${e}`);
    }
  };

  const confirmMatch = async () => {
    if (!albumPath || !release) {
      toast("Fetch the MusicBrainz release first");
      return;
    }
    setBusy(true);
    setAct({ label: `Writing MusicBrainz metadata to ${suggestions.length} track(s)…` });
    setFetchStatus("Writing MusicBrainz metadata to files…");
    try {
      await assignTracks(albumPath, release, suggestions);
      toast("MusicBrainz metadata written to files (titles, artists, album, dates, MBIDs)");
      setStep(3);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
      setFetchStatus(null);
    }
  };

  // ---------------- Step 3: covers ----------------
  /** How many tracks point at the same image (manifest art or sidecar). */
  const coverShares = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of stepTracks) if (t.cover_file) m.set(t.cover_file, (m.get(t.cover_file) ?? 0) + 1);
    return m;
  }, [stepTracks]);

  /** The release picked in the Links step, via the Cover Art Archive. */
  const mbCoverUrl = releaseId ? `https://coverartarchive.org/release/${releaseId}/front-500` : null;

  const refreshCovers = () => {
    qc.invalidateQueries({ queryKey: ["library"] });
    qc.invalidateQueries({ queryKey: ["coverInfo", albumPath] });
  };

  // Candidates the import already fetched and staged for a cover-less album
  // (`cover_review` on) — the same set the album page offers as one pick.
  // Asked only while there is no cover file, never for a covered album.
  const stagedCovers = useQuery({
    queryKey: ["stagedCovers", albumPath],
    queryFn: async () => {
      const c = await api.metadataCandidates(
        release?.artists.map((a) => a.name).join(", ") || trackArtist(stepTracks[0]?.path ?? ""),
        albumPath!,
        staged
      );
      return c.staged?.covers ?? null;
    },
    enabled: !!albumPath && !coverInfo?.file,
    retry: false,
  });
  const stagedCoverRows = stagedCovers.data?.results ?? null;

  /** Cover results: amber banner when the image is under the minimum size
   *  (the backend writes it anyway and says so), toast for the outcome.
   *  The backend compresses on write, so the toast reports the RESULTING
   *  size and what it was shrunk from. */
  const reportCover = (
    res: {
      warning?: string | null;
      width?: number; height?: number;
      below_target?: boolean;
      compressed?: boolean;
      original_width?: number | null;
      original_height?: number | null;
    },
    what: string
  ) => {
    setCoverNotice(res.warning ?? null);
    const dims = res.width && res.height ? ` (${res.width}×${res.height})` : "";
    const shrunk =
      res.compressed && res.original_width && res.width && res.original_width > res.width
        ? ` — compressed from ${res.original_width}×${res.original_height}`
        : res.compressed
          ? " — compressed"
          : "";
    toast(
      res.warning
        ? `${what} applied${dims}${shrunk} — ${res.warning}`
        : `${what} applied${dims}${shrunk}`
    );
  };

  const uploadCover = async (file: File, tracks?: string[]) => {
    if (!albumPath) return;
    if (!file.type.startsWith("image/")) {
      toast(`${file.name} is not an image — use a jpg, png or webp`);
      return;
    }
    setBusy(true);
    try {
      const res = tracks?.length
        ? await api.cover(albumPath, file, undefined, tracks, staged)
        : await api.cover(albumPath, file, undefined, undefined, staged);
      reportCover(res, tracks?.length ? `Cover assigned to ${tracks.length} track(s)` : "Album cover");
      refreshCovers();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const applyCoverUrl = async (url: string, tracks?: string[]) => {
    const u = url.trim();
    if (!albumPath || !u) return;
    setBusy(true);
    try {
      const res = tracks?.length
        ? await api.coverFromUrl(albumPath, u, undefined, tracks, undefined, staged)
        : await api.coverFromUrl(albumPath, u, undefined, undefined, undefined, staged);
      reportCover(res, tracks?.length ? `Cover assigned to ${tracks.length} track(s)` : "Album cover");
      refreshCovers();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const clearTrackCovers = async () => {
    if (!albumPath || !coverSel.size) return;
    setBusy(true);
    try {
      await api.coverClear(albumPath, [...coverSel], staged);
      toast(`Per-track cover cleared for ${coverSel.size} track(s)`);
      refreshCovers();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleCoverSel = (path: string) =>
    setCoverSel((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const setCoverSelFor = (paths: string[], on: boolean) =>
    setCoverSel((s) => {
      const next = new Set(s);
      for (const p of paths) {
        if (on) next.add(p);
        else next.delete(p);
      }
      return next;
    });

  /** The cover endpoints take audio FILENAMES, the selection holds paths. */
  const selectedCoverFiles = () => [...coverSel].map((p) => p.split("/").pop()!);

  const saveCovers = () => {
    setCoverNotice(null);
    setStep(4); // Genres
  };

  // ---------------- Step 4: genres ----------------
  // The library's own genres, for the step's autocomplete: the same cache
  // entry the genres page reads. Only asked for once the step is reached —
  // it costs a library scan server-side, which a wizard that never gets to
  // this step should not pay.
  const { data: genreFacets } = useQuery({
    queryKey: ["genreFacets"],
    queryFn: api.genresFacets,
    enabled: step === 4,
  });

  // `mb_genre_count` (Settings → Import) is the app's genre contract and the
  // server's own cap. The per-run control may LOWER it for one import, never
  // raise it: the select used to offer 4, 5 and 10, and the server trimmed
  // every one of them back to this number on the way to the file.
  const genreCap = Math.max(1, Math.min(GENRE_COUNT_MAX, Number(cfg?.mb_genre_count) || 2));
  const genreLimitValue = Math.min(genreLimit ?? genreCap, genreCap);

  /** The names the editor offers while typing, lowercase key -> the spelling
   *  to write. The library's own canonical MusicBrainz names come first (they
   *  are what the tagger writes), then the bundled families. Names no library
   *  has ever used are missing on purpose: the full MusicBrainz vocabulary is
   *  2202 entries, and a name it does not know is kept verbatim anyway (the
   *  grader flags it) rather than dropped. */
  const genreSuggestions = useMemo(() => {
    const byName = new Map<string, string>();
    const names = [...Object.keys(GENRE_FAMILIES), ...(genreFacets?.genres ?? []).map((g) => g.name)];
    for (const n of names) if (!byName.has(n.toLowerCase())) byName.set(n.toLowerCase(), n);
    return byName;
  }, [genreFacets]);

  // When the Genres step opens, prefill untouched tracks with their existing
  // GENRE tags so they are visible and editable right away.
  useEffect(() => {
    if (step !== 4) return;
    setGenres((g) => {
      let changed = false;
      const next = { ...g };
      for (const t of stepTracks) {
        if (next[t.path] === undefined && t.tags?.GENRE) {
          next[t.path] = splitGenres(t.tags.GENRE);
          changed = true;
        }
      }
      return changed ? next : g;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, stepTracks]);

  const genreList = (path: string): string[] => genres[path] ?? [];

  /** A list in the order the app WRITES it: the family first, the specific
   *  genres after it (`mlo.genres.normalize_genres`, and the slots the
   *  grader's GENRE_ORDER check reads). A list typed or read in another order
   *  is put right here, so the chips, the written tag and the grade all
   *  describe one order. */
  const familyFirst = (list: string[]): string[] => {
    const family = list.find((g) => GENRE_FAMILIES[g.toLowerCase()]);
    return family ? [family, ...list.filter((g) => g !== family)] : list;
  };

  /** Every genre currently on ANY track, with how many tracks carry it, most
   *  common first — the album-wide cleanup control renders one chip per entry. */
  const allGenres = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of stepTracks) {
      for (const gen of new Set(genreList(t.path))) m.set(gen, (m.get(gen) ?? 0) + 1);
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepTracks, genres]);

  /** What the last source run wrote, as this app renders any genre list: one
   *  name per chip, deduped on the spelling the server canonicalises to, the
   *  family in the FIRST slot the write puts it in. The run report used to
   *  print the album-wide union as one comma-joined line, which read as a
   *  single genre. */
  const jobGenres = useMemo(() => {
    const byName = new Map<string, string>();
    for (const raw of genreJobResult?.genres ?? []) {
      for (const g of splitGenres(raw)) {
        const key = canonicalGenre(g);
        if (key && !byName.has(key)) byName.set(key, g);
      }
    }
    const names = [...byName.values()];
    // `familyOf` reads a list's first slot, so ask it one name at a time — the
    // union arrives in whatever order the sources answered in.
    const family = names.find((g) => familyOf([g]));
    return family ? [family, ...names.filter((g) => g !== family)] : names;
  }, [genreJobResult]);
  /** The family chip of that report: the first slot, by construction above. */
  const jobFamily = familyOf(jobGenres);
  /** The per-source half of the report: one row per source that answered, its
   *  own names as chips beside it — the names used to be a `title` only, so
   *  they were invisible on the page. */
  const genreSourceRows = useMemo(
    () =>
      Object.entries(genreJobResult?.per_source ?? {})
        .filter(([, names]) => names.length)
        .map(([name, names]) => [name, splitGenres(names.join("; "))] as const),
    [genreJobResult]
  );

  const setGenreList = (path: string, list: string[]) =>
    setGenres((g) => ({ ...g, [path]: list }));

  const addGenre = (path: string, value: string) => {
    const typed = value.trim();
    if (!typed) return;
    // A name the vocabulary knows is written the way MusicBrainz spells it, so
    // a hand-typed genre lands identical to an imported one.
    const v = genreSuggestions.get(typed.toLowerCase()) ?? typed;
    const list = genreList(path);
    if (list.some((g) => g.toLowerCase() === v.toLowerCase())) return;
    if (list.length >= genreCap) {
      toast(`Genres per track is ${genreCap} — raise it in Settings → Import to add more`);
      return;
    }
    // A list that already opens with its family keeps that slot: inserting
    // before it would push the family down and make it look like one more
    // specific genre (the chip's own `familyOf` reads the FIRST element),
    // while the server would reorder on write anyway.
    const family = familyOf(list);
    setGenreList(path, family ? [family, ...list.slice(1), v] : [...list, v]);
  };

  const removeGenre = (path: string, genre: string) =>
    setGenreList(path, genreList(path).filter((g) => g !== genre));

  const applyGenresToDisc = (disc: number, value: string) => {
    const paths = stepTracks.filter((t) => discOfTrack(t) === disc).map((t) => t.path);
    if (!paths.length) return;
    setGenres((g) => {
      const next = { ...g };
      for (const p of paths) next[p] = familyFirst(splitGenres(value)).slice(0, genreCap);
      return next;
    });
    setDiscGenres((m) => ({ ...m, [disc]: "" }));
  };

  /** Drop one genre from EVERY track — album-wide cleanup of a bad genre. */
  const removeGenreEverywhere = (genre: string) =>
    setGenres((g) => {
      const next = { ...g };
      for (const t of stepTracks) {
        const list = next[t.path] ?? [];
        if (list.includes(genre)) next[t.path] = list.filter((x) => x !== genre);
      }
      return next;
    });

  /** Drop ALL genres from every track, leaving the step empty to re-import. */
  const removeAllGenres = () =>
    setGenres((g) => {
      const next = { ...g };
      for (const t of stepTracks) next[t.path] = [];
      return next;
    });

  const saveGenres = async () => {
    setBusy(true);
    setAct({ label: `Saving genres for ${Object.keys(genres).length} track(s)…` });
    try {
      // GENRE goes as a LIST, so the server writes one field per name. Sent as
      // the joined string it used to be, every genre list became ONE tag
      // literally named "shoegaze; rock". The family is not added here — the
      // server derives it (mlo.genres.normalize_genres) — but every list goes
      // in the order that write produces: the family first, then the
      // specifics, which is what the grader's GENRE_ORDER check reads.
      const writes: Record<string, Record<string, string | string[] | null>> = {};
      for (const [p, list] of Object.entries(genres))
        writes[p] = { GENRE: list.length ? familyFirst(list) : null };
      await api.mbAssign(writes, staged);
      toast("Genres saved");
      setStep(5);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  // ---------------- Step 5: lyrics ----------------
  // Display name for a track ANYWHERE in the wizard: MusicBrainz release
  // title > existing tag > filename-derived fallback.
  const displayTitle = (p: string) => {
    const rt = suggByPath.get(p)?.release_track;
    if (rt?.title) return rt.title;
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tags?.TITLE ?? defaultTrackName(p);
  };

  const trackArtist = (p: string) => {
    const rt = suggByPath.get(p)?.release_track;
    if (rt?.artist_credit) return rt.artist_credit;
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tags?.ARTIST ?? "";
  };
  const trackTitle = (p: string) => {
    const rt = suggByPath.get(p)?.release_track;
    if (rt?.title) return rt.title;
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tags?.TITLE ?? defaultTrackName(p);
  };
  const trackDuration = (p: string): number | undefined => {
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tech?.length ? Math.round(t.tech.length) : undefined;
  };
  const trackAlbum = release?.title || stepTracks[0]?.tags.ALBUM || undefined;

  /** The richest row for a path: the library payload first, the step's own
   *  rows (suggestions / folder scan) second. */
  const findTrack = (p: string): Track | undefined =>
    trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);

  /** (disc, track) digits from a leading "D-TT" / "TT" file name — the same
   *  fallback the library payload derives for untagged files. */
  const numsFromName = (file: string): { disc: number | null; track: number | null } => {
    const stem = (file.split("/").pop() ?? "").replace(/\.[^.]+$/, "");
    const dd = stem.match(/^\s*(\d+)\s*-\s*(\d+)/);
    if (dd) return { disc: Number(dd[1]), track: Number(dd[2]) };
    const tt = stem.match(/^\s*(\d+)/);
    return { disc: null, track: tt ? Number(tt[1]) : null };
  };

  /** Track number shown on EVERY step: the matched MusicBrainz position when
   *  there is one (that is the number written to the file), else the local tag,
   *  else the number derived from the file name. */
  const trackNoOf = (p: string): number | null => {
    const m = suggByPath.get(p)?.release_track;
    if (m) return m.position;
    const t = findTrack(p);
    if (!t) return null;
    return parseDisc(t.tags?.TRACKNUMBER) ?? t.tracknumber ?? numsFromName(t.file || p).track;
  };

  /** Disc number shown on EVERY step, same precedence as {@link trackNoOf}. */
  const discNoOf = (p: string): number | null => {
    const m = suggByPath.get(p)?.release_track;
    if (m) return m.disc;
    const t = findTrack(p);
    if (!t) return null;
    return parseDisc(t.tags?.DISCNUMBER) ?? t.discnumber ?? numsFromName(t.file || p).disc;
  };

  /** Whether this track will actually carry lyrics once the step is saved.
   *  A checkmark used to appear for any track whose .lrc sidecar merely
   *  EXISTED, so empty sidecars from an aborted run claimed lyrics they did
   *  not have. Instrumentals never count. */
  const hasLyrics = (t: Track): boolean => {
    if ((instrumental[t.path] ?? t.tags.INSTRUMENTAL) === "1") return false;
    const draft = lyricsDrafts[t.path];
    if (draft && draft.trim()) return true;   // what this step is about to write
    return !!(t.lyrics_embedded || t.lyrics_lrc); // what is already on disk
  };

  /** Auto-import lyrics through the configured provider chain (script 13's own
   *  engine): one track from a track row, the whole step from the header.
   *  Reports the winning provider per track plus skipped/failed counts — the
   *  chain order lives in Settings → Lyrics. */
  const autoImportLyrics = async (paths?: string[]) => {
    const targets = paths ?? stepTracks.map((t) => t.path);
    if (!targets.length) {
      toast("No tracks to fetch lyrics for");
      return;
    }
    setBusy(true);
    try {
      // Chunked and COUNTED. One POST for the whole album left the strip
      // indeterminate, and because the server writes each track as it goes,
      // the row kept reading "Fetching lyrics for 16 track(s)…" long after
      // the lyrics were on disk (the report this fixes). Eight at a time
      // keeps each request short and lets the bar say where the batch is.
      const CHUNK = 8;
      let ok = 0;
      let skipped = 0;
      let failed = 0;
      const byProvider: Record<string, number> = {};
      for (let i = 0; i < targets.length; i += CHUNK) {
        const slice = targets.slice(i, i + CHUNK);
        setAct({
          label:
            targets.length === 1
              ? `Fetching lyrics for ${displayTitle(targets[0])}…`
              : `Fetching lyrics — ${i}/${targets.length}…`,
          done: i,
          total: targets.length,
        });
        const res = await api.lyricsAuto(slice, false, staged);
        ok += res.ok;
        skipped += res.skipped;
        failed += res.failed;
        for (const r of res.results) {
          if (r.status === "ok" && r.provider_label)
            byProvider[r.provider_label] = (byProvider[r.provider_label] ?? 0) + 1;
        }
        // The rows tick as each chunk lands: the per-track marks are what
        // this step is for, and they come from the fetch itself.
        setLyrResults((m) => {
          const next = { ...m };
          for (const r of res.results) next[r.path] = r;
          return next;
        });
      }
      // The fetch is over — the lyrics are on disk. What follows is a second
      // server pass (the folder is re-read and re-graded); it wears its own
      // label so the strip can never claim it is still fetching.
      setAct({
        label: "Reading the album back after the fetch…",
        done: targets.length,
        total: targets.length,
      });
      const got = Object.entries(byProvider).map(([label, n]) => `${label} ${n}`).join(", ");
      const rest = [
        skipped ? `${skipped} Skipped` : "",
        failed ? `${failed} Failed` : "",
      ].filter(Boolean).join(", ");
      toast(
        ok
          ? `Lyrics written for ${ok} track(s)${got ? ` — ${got}` : ""}${rest ? ` (${rest})` : ""}`
          : `No new lyrics found${rest ? ` — ${rest}` : ""}`
      );
      qc.invalidateQueries({ queryKey: ["library"] });
      // The files changed on disk: re-read them so the step's "Lyrics" marks
      // come from what the fetch actually wrote.
      await rescanTracks();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  /** The lyrics chain's other two halves, by hand, over this album's own
   *  tracks. Both call the entry point the chain calls, so nothing here can
   *  write something an import would not:
   *
   *  * `xlit` — script 17's runner: `TRANSLITERATION-<lang>` /
   *    `TRANSLATION-<lang>` tags for the lyrics that need them, plus the
   *    `.romaji.lrc` / `.<lang>.lrc` sidecars the LRC formats write (and the
   *    same stale-transform cleanup a re-run does);
   *  * `publish` — script 18's per-track core: submit what LRCLIB does not
   *    have yet. It writes NOTHING locally, so the counts and LRCLIB's own
   *    answer are all there is to report.
   *
   *  A pass that changed files is re-read so the step's "Lyrics" marks come
   *  from what it wrote, and a pass that wrote nothing says why (both
   *  switches off, no AI configured) instead of looking like it did. */
  const runLyricsPass = async (kind: "xlit" | "publish") => {
    // Neither pass has anything to do with an instrumental — the runner skips
    // them too, so they are not even sent.
    const targets = stepTracks
      .filter((t) => (instrumental[t.path] ?? t.tags.INSTRUMENTAL) !== "1")
      .map((t) => t.path);
    if (!targets.length) {
      toast("Nothing to work on — every track is marked INSTRUMENTAL");
      return;
    }
    setBusy(true);
    setLyrPass(null);
    setAct({
      label:
        kind === "xlit"
          ? `Transliterating / translating lyrics for ${targets.length} track(s)…`
          : `Publishing lyrics to LRCLIB for ${targets.length} track(s)…`,
    });
    try {
      if (kind === "xlit") {
        const res = await api.lyricsXlit(targets, false, staged);
        const detail = [
          `${res.ok} file(s) updated`,
          res.skipped ? `${res.skipped} unchanged` : "",
          res.errors.length ? `${res.errors.length} failed — ${res.errors[0]}` : "",
          res.note,
        ].filter(Boolean).join(" · ");
        setLyrPass({ kind, text: detail, failed: res.errors.length > 0 });
        if (res.errors.length) toast.error(`Transliteration — ${res.errors[0]}`);
        else toast(detail);
        // The tags changed on disk: re-read them so the step shows what the
        // pass actually wrote (an unchanged run costs nothing).
        await rescanTracks();
      } else {
        const res = await api.lyricsPublishBatch(targets, false, staged);
        const reasons = new Map<string, number>();
        for (const r of res.results) {
          if (r.status === "skipped") {
            const why = r.reason || "skipped";
            reasons.set(why, (reasons.get(why) ?? 0) + 1);
          }
        }
        const fails = res.results.filter((r) => r.status === "failed");
        const detail = [
          `${res.ok} submitted`,
          ...[...reasons].map(([why, n]) => `${n} × ${why}`),
          fails.length ? `${fails.length} failed — ${fails[0].reason || fails[0].message || "no message"}` : "",
        ].filter(Boolean).join(" · ");
        setLyrPass({ kind, text: detail, failed: fails.length > 0 });
        if (fails.length) toast.error(`LRCLIB publish — ${fails[0].reason || fails[0].message || "failed"}`);
        else toast(detail);
      }
    } catch (e) {
      setLyrPass({ kind, text: String(e), failed: true });
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  const saveLyricsStep = async () => {
    setBusy(true);
    setAct({ label: `Saving lyrics & INSTRUMENTAL for ${stepTracks.length} track(s)…` });
    try {
      // Write what the config asks for — the wizard used to always write .lrc.
      const cfg = await api.config();
      const fmt = String(cfg.lyrics_format ?? "EMBEDDED").toUpperCase(); // EMBEDDED | LRC | BOTH
      const writes: Record<string, Record<string, string | null>> = {};
      let embedded = 0;
      let sidecars = 0;
      let untimed = 0;
      for (const t of stepTracks) {
        const inst = instrumental[t.path] ?? (t.tags.INSTRUMENTAL === "1" ? "1" : "0");
        writes[t.path] = { INSTRUMENTAL: inst };
        if (inst === "1") continue; // instrumentals never carry lyrics
        const lrc = lyricsDrafts[t.path];
        if (!lrc || !lrc.trim()) continue;
        if (!parseLrc(lrc).length) {
          untimed++; // plain text: nothing to timestamp, nothing written
          continue;
        }
        if (fmt === "LRC" || fmt === "BOTH") {
          await api.lyricsWrite(t.path, lrc, staged);
          sidecars++;
        }
        if (fmt === "EMBEDDED" || fmt === "BOTH") {
          await api.lyricsEmbed(t.path, lrc, staged);
          embedded++;
        }
      }
      await api.mbAssign(writes, staged);
      setLyricsNotice(
        untimed ? `${untimed} track(s) have lyrics without timestamps — not saved as .lrc` : null
      );
      toast(`Lyrics format ${fmt}: ${embedded} Embedded, ${sidecars} .lrc — INSTRUMENTAL saved`);
      setStep(6);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  // ---------------- Step 6: advisory ----------------
  /** Fetch the advisory rating for EVERY track of this album, now.

   *  One call for the whole album, through the same advisory source path the
   *  album page's Check button uses (`/api/mb/advisory/fetch`): the server
   *  keys Apple's explicit-edition album route on the folder's track count,
   *  so a per-track loop would degrade the answers it can get. The reply's
   *  per-track `values`/`sources`/`answers`/`status` are the outcome rows
   *  below, and a failure is shown rather than swallowed.

   *  `force` is the re-rate: the server echoes a file that already carries a
   *  valid 0/1/2 instead of asking anyone, and `force` asks anyway and writes
   *  what the sources state — the ONLY route that can lower a rating (a 0 an
   *  earlier run invented outlives every provider that later knew better), so
   *  it is only ever reached through the confirmed button below. */
  const fetchAdvisoryAll = async (force = false) => {
    const targets = stepTracks.map((t) => t.path);
    if (!targets.length) {
      toast("No tracks to fetch an advisory for");
      return;
    }
    setBusy(true);
    setAdvError(null);
    setAct({
      label: force
        ? `Re-rating ${targets.length} track(s) — asking the sources anyway…`
        : `Asking the advisory sources for ${targets.length} track(s)…`,
    });
    try {
      const res = await api.mbAdvisoryFetch({ paths: targets, staged, force });
      setAdvReply(res);
      // The server wrote what it found — mirror it into the step's buttons so
      // they show the fetched value, not the pre-fetch tag.
      setAdvisory((a) => {
        const next = { ...a };
        for (const p of targets) {
          const v = replyFor(res.values, p);
          if (v !== undefined) next[p] = String(v);
        }
        return next;
      });
      const answered = targets.filter((p) => replyFor(res.answers, p)).length;
      // `advisoryOutcome` leads, not a bare count: a reply that wrote nothing
      // says WHY (already rated, re-checked and unchanged, or gated) instead of
      // reading like a re-rate that found nothing.
      toast(`${advisoryOutcome(res)} — ${answered} of ${targets.length} track(s) had a source answer`);
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["album"] });
    } catch (e) {
      setAdvError(String(e));
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  /** The re-rate, behind a confirmation: it asks for tracks the server would
   *  otherwise leave alone and can LOWER a rating, which is not something a
   *  stray click may do. */
  const reRateAdvisoryAll = () => {
    if (!stepTracks.length) return;
    if (
      !window.confirm(
        `Re-rate ITUNESADVISORY for ${stepTracks.length} track(s) and write what the sources state?\n\n` +
          "This asks even for files that already carry a value, and a source's answer can lower a rating (1 → 0). " +
          "The album tag ALBUMITUNESADVISORY is derived from the new values."
      )
    )
      return;
    void fetchAdvisoryAll(true);
  };

  const applyAdvisoryToAll = (v: string) => {
    setAdvisory((a) => {
      const next = { ...a };
      for (const t of stepTracks) next[t.path] = v;
      return next;
    });
  };

  const saveAdvisory = async () => {
    setBusy(true);
    setAct({ label: `Saving advisory for ${stepTracks.length} track(s)…` });
    try {
      // Only write tracks the user explicitly changed — untouched tracks keep
      // their existing ITUNESADVISORY tags instead of being wiped.
      const writes: Record<string, Record<string, string | null>> = {};
      for (const t of stepTracks) {
        const picked = advisory[t.path];
        if (picked === undefined) continue;
        const existing = t.tags?.ITUNESADVISORY ?? undefined;
        if (picked !== existing) writes[t.path] = { ITUNESADVISORY: picked };
      }
      if (!Object.keys(writes).length) {
        toast("No advisory changes — pick values or use Apply to all");
        setStep(7);
        return;
      }
      await api.mbAssign(writes, staged);
      toast("Advisory ratings saved");
      setStep(7);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAct(null);
      setBusy(false);
    }
  };

  // The Done step's script list comes from lib/scripts.ts — the single source
  // of truth every other script menu in the app already uses. This was an
  // 8-entry list hardcoded here, and it silently drifted: AccurateRip, Format
  // all, Remux videos, Key & BPM, Fetch lyrics and Beets were all missing.
  // The default ticks were a hand-kept five of their own ([1, 2, 5, 7, 4]),
  // which is neither the set nor the ORDER any other Run All surface uses —
  // Done then ran Grade (4) before the scripts that write the tags it grades.
  // They are now the app's own run order: run_all_order when the user set one,
  // DEFAULT_RUN_ALL otherwise — the same list, in the same order, the
  // Optimization page and the library's Run All run.
  const postImportOrder = Array.isArray(cfg?.run_all_order) && (cfg.run_all_order as number[]).length
    ? (cfg.run_all_order as number[]).filter(isScriptId)
    : DEFAULT_RUN_ALL;
  // The boxes are laid out in that order, and a script the user's order does
  // not name still gets its box (at its factory position) instead of dropping
  // off the list — the same completion the Settings page's Run All grid makes.
  const POST_IMPORT_SCRIPTS = [
    ...postImportOrder,
    ...DEFAULT_RUN_ALL.filter((id) => !postImportOrder.includes(id)),
  ].map((id) => ({ id, label: SCRIPT_LABEL[id] ?? `#${id}` }));
  // Null = the boxes have not been touched, so they follow the configured
  // order — which the config answers only after the first render.
  const [runAfterImport, setRunAfterImport] = useState<number[] | null>(null);
  const runAfterImportIds = runAfterImport ?? postImportOrder;
  const [scriptsRunning, setScriptsRunning] = useState(false);
  const [runningAll, setRunningAll] = useState(false);
// Last action's outcome, shown in the step: a toast is gone by the time you
// look back at a chain that took a minute to run.
const [finishMsg, setFinishMsg] = useState<string | null>(null);
// One row per chain id of the last run (null = nothing run here yet). A
// failing script is a row with its own error text, not just a count.
const [runRows, setRunRows] = useState<RunRow[] | null>(null);

/** A run's per-script results as report rows — the chain's own labels when it
 *  reports them, the wizard's script list otherwise. */
const rowsFromResults = (results: ScriptRunResult[]): RunRow[] =>
  results.map((r) => ({
    id: r.id,
    label: r.label ?? r.name ?? SCRIPT_LABEL[r.id] ?? `Script ${r.id}`,
    ok: !r.error && !r.skipped,
    skipped: !!r.skipped,
    error: r.error ? String(r.error) : undefined,
    note: r.reason,
  }));

/** The user's own Run All — config.run_all_order, the same order the
 *  Optimization page runs — aimed at this wizard's album(s) only. */
const runAllHere = async () => {
  const targets = albumTargets();
  if (!targets.length) {
    toast("Nothing imported yet");
    return;
  }
  setRunningAll(true);
  setFinishMsg("Running all scripts…");
  setRunRows(null);
  try {
    // The boxes' own order — the app's Run All order, aimed at this wizard's
    // album(s) only.
    const order = postImportOrder;
    setAct({ label: `Run all scripts — ${order.length} script(s) on ${targets.length} album(s)` });
    const res = await api.run(order, targets);
    const rows = rowsFromResults(res.results ?? []);
    setRunRows(rows);
    const failed = rows.filter((r) => !r.ok && !r.skipped);
    if (failed.length) toast.error(`${failed.length} script(s) failed — see the step`);
    else toast.success("Run All finished");
    setFinishMsg(
      failed.length
        ? `Run all: ${failed.length} of ${order.length} script(s) failed — ${failed[0].error}`
        : `Run all: ${order.length} script(s) finished on ${targets.length} album${targets.length > 1 ? "s" : ""}`
    );
  } catch (e) {
    setFinishMsg(`Run all failed — ${String(e)}`);
    toast.error(String(e));
  } finally {
    setAct(null);
    setRunningAll(false);
    qc.invalidateQueries({ queryKey: ["library"] });
    qc.invalidateQueries({ queryKey: ["album"] });
    qc.invalidateQueries({ queryKey: ["coverInfo", albumPath] });
  }
};

/** Run every configured post-import script on the new album(s) right now,
 *  without leaving the wizard: api.importFinish is the same chain the bulk
 *  queue and the Soulseek import run, and reports per-script errors. The
 *  checkboxes stay the "on Done" shortcut for a chosen subset.
 *
 *  Targets the album the wizard was opened on too (?album=), which is exactly
 *  the album an auto-import drops you into with nothing "uploaded". */
const runAllScripts = async () => {
  const targets = albumTargets();
  if (!targets.length) {
    toast("Import the files first — the chain runs on an imported album");
    return;
  }
  setScriptsRunning(true);
  setFinishMsg("Running the import chain…");
  setRunRows(null);
  let stillMissing: string[] = [];
  try {
    setAct({ label: `Import chain — ${scriptChain?.chain?.length ?? 0} script(s) on ${targets.length} album(s)` });
    const res = await api.importFinish(targets, {}, staged);
    // The chain's beets tagging / organize steps rename the album folder to
    // its canonical layout, so the path this wizard holds can be gone by the
    // time the reply lands. The reply carries the folder the album is in NOW
    // (server-side re-resolution), and it is in the same order as `targets`:
    // adopt it, or "Open album" and every later step points at a directory
    // that no longer exists.
    if (res.albums.length) {
      setUploaded((prev) =>
        prev.length === res.albums.length
          ? prev.map((u, i) => (res.albums[i]?.path ? { ...u, path: res.albums[i].path } : u))
          : prev
      );
      const mine = res.albums[Math.min(albumIndex, res.albums.length - 1)];
      if (mine?.path) setAlbumPath(mine.path);
      // What the chain still could not finish, from the reply's own autonomy
      // block (the same families the notification names): the banner then
      // points at the step that decides each one instead of leaving the album
      // looking finished.
      stillMissing = Object.keys(mine?.autonomy?.missing ?? {}).filter((id) => id in FAMILY_STEP);
      setMissingFamilies(stillMissing);
      qc.invalidateQueries({ queryKey: ["importPrompts"] });
    }
    // The chain reports one result per chain id per album; the album is kept
    // in the label so a multi-album queue stays readable.
    const rows = res.albums.flatMap((a) =>
      rowsFromResults((a.scripts ?? []) as ScriptRunResult[]).map((r) => ({
        ...r,
        label: res.albums.length > 1 ? `${baseName(a.path) || a.path} · ${r.label}` : r.label,
      }))
    );
    setRunRows(rows);
    const errors = res.albums.flatMap((a) =>
      a.errors.map((e) => `${baseName(a.path) || a.path}: ${String(e)}`)
    );
    const failed = rows.filter((r) => !r.ok && !r.skipped).length;
    toast(
      errors.length || failed
        ? `Import chain finished with ${failed || errors.length} script error(s): ${errors.slice(0, 3).join("; ") || rows.find((r) => r.error)?.error}`
        : `Import chain finished on ${targets.length} album(s) — progress shows at the top of the window`
    );
    setFinishMsg(
      errors.length
        ? `Import chain: ${errors.length} script error(s) — ${errors.slice(0, 3).join("; ")}`
        : `Import chain finished on ${targets.length} album${targets.length > 1 ? "s" : ""}` +
          (stillMissing.length
            ? ` — still missing ${stillMissing.map((id) => FAMILY_LABEL[id]).join(", ")}`
            : "")
    );
    qc.invalidateQueries({ queryKey: ["library"] });
    qc.invalidateQueries({ queryKey: ["album"] });
    qc.invalidateQueries({ queryKey: ["coverInfo", albumPath] });
  } catch (e) {
    setFinishMsg(`Import chain failed — ${String(e)}`);
    toast.error(String(e));
  } finally {
    setAct(null);
    setScriptsRunning(false);
  }
};

const finish = async () => {
  try {
    // Same targets as the chain: an album opened via ?album= is just as real
    // an import, it simply has nothing "uploaded".
    const targets = albumTargets();
    if (runAfterImportIds.length && targets.length) {
      await api.run(runAfterImportIds, targets);
    }
  } catch (e) {
    toast.error(String(e));
  }
  qc.invalidateQueries({ queryKey: ["library"] });
  qc.invalidateQueries({ queryKey: ["importPrompts"] });
  setParams({});
  toast(uploaded.length > 1 ? `Imported ${uploaded.length} albums — enrich each from its album page` : "Import complete — album graded");
};

  const switchAlbum = (i: number) => {
    setAlbumIndex(i);
    setAlbumPath(uploaded[i].path);
    autoDetected.current = { path: null, done: false };
    setDetectedFromTags(false);
    setRelease(null);
    setReleaseId("");
    setSuggestions([]);
    setGenres({});
    setDiscGenres({});
    setMbLink("");
    setRymLink("");
    setRymValid(null);
    setRymKind(null);
    setRymArtistValid(null);
    setRymArtistKind(null);
    setRymArtistNote("");
    setLyrOpen(new Set());
    setRymNote("");
    setRymArtistLink("");
    setSearchHits([]);
    // reset per-track drafts so album B never inherits album A's data
    setLyricsDrafts({});
    setInstrumental({});
    setAdvisory({});
    setCoverNotice(null);
    setLyricsNotice(null);
    setLyrResults({});
    setLyrPass(null);
    setCoverSel(new Set());
    setCoverUrl("");
    setTrackCoverUrl("");
    setCoverSearch(null);
    // Results fetched for album A must not sit on album B's steps: the
    // metadata rows, the advisory/genre answers, the per-script run report
    // and the fingerprint match are all per-album reads.
    setMetaReply(null);
    setMetaError(null);
    setAdvReply(null);
    setAdvError(null);
    setGenreJobResult(null);
    setGenreError(null);
    setRunRows(null);
    setFinishMsg(null);
    setAcoustid(null);
  };

  /** Open the album a prompt is about, on the step that needs the decision.
   *  The missing list goes back into the URL so a reload keeps landing on the
   *  same step with the same warning. */
  const openPrompt = (p: ImportPrompt) => {
    const ids = p.families.map((f) => f.id).filter((id) => id in FAMILY_STEP);
    setMissingFamilies(ids);
    setAlbumPath(p.album);
    setStep(stepFromParam(null, ids) ?? 1);
    setParams({ album: p.album, step: FAMILY_STEP[ids[0]] ?? "", missing: ids.join(",") });
    qc.invalidateQueries({ queryKey: ["album"] });
  };

  /** Stop asking about one album. It is not "resolved" — the next import of
   *  the same album recomputes the gaps and raises the prompt again. */
  const dismissPrompt = async (album: string) => {
    try {
      await api.dismissImportPrompt(album, !inMusicFolder(album, cfg?.music_folder));
    } catch (e) {
      toast.error(String(e));
    }
    if (albumPath === album) setMissingFamilies([]);
    qc.invalidateQueries({ queryKey: ["importPrompts"] });
  };

  /** Hide this album's missing banner without touching the queue entry: the
   *  user is on the album and does not want the reminder while they work. */
  const hideMissing = () => {
    setMissingFamilies([]);
    setParams(albumPath ? { album: albumPath } : {});
  };

  const missingHere = missingFamilies.find((id) => FAMILY_STEP[id] === STEPS[step]) ?? null;
  // Minimum entry: this visit came from a prompt's own link, so the steps ask
  // for what is missing and nothing else. The live list, not the URL param:
  // answering the last gap (or Dismiss) ends the mode and hands the ordinary
  // wizard back.
  const minMode = missingFamilies.length > 0;

  const totalFiles = albums.reduce((n, g) => n + g.files.length, 0);
  // Queue panel rows: the staged albums, else what step 0 is about to import.
  const queueItems: { name: string; path: string }[] = uploaded.length
    ? uploaded
    : albums.filter((g) => g.files.length).map((g) => ({ name: g.name.trim() || "Album", path: "" }));
  const hasRipFiles = albums.some((g) => g.files.some((f) => /\.(cue|log|accurip)$/i.test(f.relPath)));
  // What step 0 is about to import: the staged files, every album named. The
  // later steps open on it too — a step past the first needs an album.
  const albumsReady =
    totalFiles > 0 && albums.length > 0 && albums.every((g) => g.name.trim() || g.files.length === 0);
  const canNext =
    step === 0
      ? albumsReady
      : step === 1
        ? !!(releaseId || extractMbid(mbLink))
        : true;
  // Why Continue is disabled — shown next to the button instead of leaving
  // the user with a dead button.
  const nextBlock = (() => {
    if (canNext) return null;
    if (step === 0)
      return totalFiles === 0
        ? "Add files or pick a folder first"
        : !albums.length
          ? "Nothing to import"
          : "Every album needs a name";
    if (step === 1) return "Enter a MusicBrainz release URL or ID first";
    return null;
  })();

  /** Why a step ahead of the current one is not a jump yet: the data its body
   *  reads is not there, and the jump would land on an empty form (step 3
   *  renders nothing at all without an album, step 2 only a "no data" line).
   *  The steps behind the current one are never gated — they were walked. */
  const stepGate = (i: number): string | null => {
    if (i <= step) return null;
    if (i === 1 && !albumPath && !albumsReady)
      return "Add the album's files on the first step first";
    if (i === 2 && !release && !suggestions.length)
      return "Fetch the MusicBrainz release on the Links step first";
    if (i >= 3 && !albumPath)
      return "Import the album first — these steps read and write the files on disk";
    return null;
  };

  // Manual importing is off (Settings → Import pipeline): every importing
  // /api/import/* call answers 409 with this same sentence, so the wizard says
  // it once, in place of eight steps whose every button would come back
  // refused. The user still gets the album link and the queue's own pages —
  // only the path they drive by hand is closed. The words mirror
  // mlo/import_policy.MANUAL_OFF_NOTE, which is what the API answers with.
  if (cfg && cfg.manual_import_enabled === false) {
    return (
      <div className="p-6 space-y-5 mx-auto max-w-6xl">
        <PageHeader
          icon={UploadCloud}
          title="Import"
          subtitle={albumPath ? (
            <span className="truncate">
              <Link to={`/album/${encodeURIComponent(albumPath)}`} className="hover:text-accent-soft">
                {albumPath.split("/").pop()}
              </Link>
            </span>
          ) : undefined}
        />
        <div className="panel p-6 space-y-2">
          <div className="flex items-center gap-2 text-sm font-semibold text-amber-200">
            <AlertTriangle className="h-4 w-4 shrink-0" /> Importing by hand is off
          </div>
          <p className="text-sm text-zinc-400">
            Importing by hand is off (manual_import_enabled) — turn it back on in
            Settings → Import pipeline to import albums yourself.
          </p>
          <p className="text-xs text-zinc-500">
            Nothing was imported. Downloads that finish are still imported by the automatic
            pipeline, and this album's page and the queue stay available.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={UploadCloud}
        title="Import"
        subtitle={
          uploaded.length > 1 || albumPath ? (
            <>
              {uploaded.length > 1 && (
                <select className="input !w-auto text-sm tap" value={albumIndex} onChange={(e) => switchAlbum(Number(e.target.value))}>
                  {uploaded.map((a, i) => (
                    <option key={a.path} value={i}>{a.name}</option>
                  ))}
                </select>
              )}
              {albumPath && (
                <span className="truncate">
                  <Link to={`/album/${encodeURIComponent(albumPath)}`} className="hover:text-accent-soft">
                    {albumPath.split("/").pop()}
                  </Link>
                </span>
              )}
            </>
          ) : undefined
        }
      />

      {/* ---- imports waiting on a decision -------------------------------
          One row per album an import could not finish (server.import_prompts,
          raised by the same call that raises the notification). "Decide" opens
          the album at the step that answers it; "Dismiss" stops the asking and
          is not a resolution — the next import of the album recomputes it. */}
      {importPrompts.length > 0 && (
        <div className="panel p-3 space-y-1.5">
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
            Imports waiting on you ({importPrompts.length})
          </div>
          {importPrompts.map((p) => (
            <div key={p.album} className="flex items-center gap-2 flex-wrap text-xs">
              <span className="text-zinc-200 truncate max-w-[16rem]">{p.album_name}</span>
              <span className="text-amber-300/90 truncate">
                {p.families.map((f) => f.label || FAMILY_LABEL[f.id] || f.id).join(", ")}
                {p.reason === "stopped" ? " — import stopped there" : ""}
              </span>
              <div className="ml-auto flex items-center gap-1.5">
                <button className="btn-primary !py-1 !px-2 text-[11px] tap" onClick={() => openPrompt(p)}>
                  Decide
                </button>
                <button
                  className="btn-ghost !py-1 !px-2 text-[11px] tap"
                  title="Stop asking about this album (a later import of it asks again if something is still missing)"
                  onClick={() => dismissPrompt(p.album)}
                >
                  Dismiss
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* step indicator — every step is a jump, in both directions, like the
          setup wizard's own rail. The strip used to swallow a forward click
          (`i < step && setStep(i)`), which reads as a dead button; a step
          whose body needs data that is not there yet stays disabled and says
          what it is waiting for, so a jump never lands on an empty form. */}
      <div className="flex items-center gap-1.5 overflow-x-auto">
        {STEPS.map((s, i) => {
          const waiting = stepGate(i);
          return (
            <div key={s} className="flex items-center gap-1.5 shrink-0">
              <button
                onClick={() => setStep(i)}
                disabled={busy || !!waiting}
                title={waiting ?? undefined}
                className={`flex items-center gap-1.5 rounded-lg px-3 py-1 text-xs tap transition-colors ${
                  i === step
                    ? "bg-accent on-accent"
                    : i < step
                      ? "bg-accent/10 text-accent-soft hover:bg-accent/20"
                      : waiting
                        ? "bg-raise text-zinc-600 border border-border opacity-60 cursor-not-allowed"
                        : "bg-raise text-zinc-400 border border-border hover:text-white"
                }`}
              >
                {i < step ? <Check className="h-3 w-3" /> : <span>{i + 1}</span>}
                {s}
              </button>
              {i < STEPS.length - 1 && <div className="h-px w-3 bg-border" />}
            </div>
          );
        })}
      </div>

      {/* ---- what this album is still missing -----------------------------
          The prompt's own banner: the families an import could not decide,
          each one a click from the step that answers it. It stays until the
          album is imported again (a resolved gap clears it) or the user
          dismisses the prompt. */}
      {missingFamilies.length > 0 && (
        <div className="panel px-3 py-2 text-xs">
          <div className="flex items-center gap-2 flex-wrap">
            <AlertTriangle className="h-3.5 w-3.5 text-amber-300 shrink-0" />
            <span className="text-amber-200">
              This import could not finish: {missingFamilies.map((id) => FAMILY_LABEL[id]).join(", ")} still missing
            </span>
            <span className="text-zinc-500">Decide each one, then Finish.</span>
            <div className="ml-auto flex items-center gap-1.5">
              {missingFamilies.map((id) => {
                const i = STEPS.indexOf(FAMILY_STEP[id]);
                return (
                  <button
                    key={id}
                    className={`!py-1 !px-2 text-[11px] tap rounded-lg border border-border ${
                      i === step ? "bg-accent on-accent" : "bg-raise text-zinc-300 hover:text-white"
                    }`}
                    onClick={() => i >= 0 && setStep(i)}
                  >
                    {FAMILY_LABEL[id]}
                  </button>
                );
              })}
              <button className="btn-ghost !py-1 !px-2 text-[11px] tap" onClick={hideMissing}>
                Hide
              </button>
              <button
                className="btn-ghost !py-1 !px-2 text-[11px] tap"
                title="Stop asking about this album (a later import of it asks again if something is still missing)"
                onClick={() => albumPath && dismissPrompt(albumPath)}
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ---- the wizard's one progress strip -------------------------------
          Every action below reports here: the action's own step count when it
          has one, else the relay frame a script/import run publishes (the same
          websocket the header bar draws), else an indeterminate, ticking bar.
          A disabled button with nothing moving is what "looks stuck" was. */}
      {(busy || act) && (
        <div className="panel px-3 py-2">
          <ActionBar
            active
            label={act?.label ?? progress?.desc ?? fetchStatus ?? "Working…"}
            done={act?.done ?? progress?.done}
            total={act?.total ?? progress?.total}
            steps={act ? null : progress?.steps}
          />
        </div>
      )}

      {/* ---------------- Queue mode: several albums at once ---------------- */}
      {queueMode && (
        <div className="panel p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-zinc-200 flex items-center gap-1.5">
              <Disc3 className="h-4 w-4 text-accent" /> Queue mode — {queueItems.length} album(s)
            </span>
            {bulkJob?.status === "running" && (
              <span className="text-xs text-zinc-400 flex items-center gap-1.5">
                <span className="h-3 w-3 rounded-full border-2 border-zinc-700 border-t-accent-soft animate-spin shrink-0" />
                <span className="truncate max-w-[16rem]" title={bulkJob.label}>{bulkJob.label || "Importing…"}</span>
                <span className="font-mono tabular-nums shrink-0">
                  {fmtCounts(bulkJob.done ?? 0, bulkJob.total ?? queueItems.length)}
                </span>
              </span>
            )}
            {bulkJob?.status === "done" && (
              <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">
                <Check className="h-3 w-3" /> queue done — {fmtCounts(bulkJob.done ?? 0, bulkJob.total ?? queueItems.length)}
              </span>
            )}
            {bulkJob?.status === "failed" && (
              <span className="chip bg-red-900/40 text-red-300 border border-red-900">queue failed</span>
            )}
            <span className="text-xs text-zinc-500 ml-auto">
              {uploaded.length
                ? "The 8 steps below run on the album picked in the dropdown; shared actions cover the whole queue."
                : "Importing runs the queue; each album can then be walked through the 8 steps."}
            </span>
          </div>
          <ScriptChainNote preview={scriptChain} />
          <div className="space-y-1">
            {queueItems.map((it, i) => {
              const row = it.path ? bulkJob?.items?.find((r) => r.path === it.path) : undefined;
              const state = row?.status ?? "queued";
              return (
                <div
                  key={it.path || `new-${i}`}
                  className={`${ROW} text-xs`}
                >
                  <Disc3 className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                  {uploaded.length ? (
                    <button
                      className={`flex-1 min-w-0 text-left truncate ${
                        i === albumIndex ? "text-accent-soft" : "text-zinc-300 hover:text-accent-soft"
                      }`}
                      onClick={() => switchAlbum(i)}
                      title="Work this album through the steps below"
                    >
                      {it.name}
                    </button>
                  ) : (
                    <span className="flex-1 min-w-0 truncate text-zinc-300">{it.name}</span>
                  )}
                  {row?.error && (
                    <span className="text-[10px] text-red-300/90 truncate max-w-[40%] sm:max-w-[18rem]" title={row.error}>
                      {row.error}
                    </span>
                  )}
                  <span
                    className={`chip bg-raise border border-border shrink-0 ${
                      state === "imported"
                        ? "text-emerald-300"
                        : state === "failed"
                          ? "text-red-300"
                          : state === "skipped"
                            ? "text-amber-300"
                            : "text-zinc-500"
                    }`}
                  >
                    {QUEUE_STATE_LABEL[state] ?? state}
                  </span>
                </div>
              );
            })}
          </div>
          {bulkJob?.status === "failed" && bulkJob.error && (
            <div className="text-xs text-red-300">Queue import failed: {bulkJob.error}</div>
          )}
          <AcoustidBlock
            match={acoustid}
            busy={acoustidBusy}
            queue
            canMatchAll={!!(releaseId || extractMbid(mbLink))}
            matchAllBusy={matchAllBusy}
            applied={acoustidApplied}
            onRun={runAcoustid}
            onUse={useAcoustidRelease}
            onSubmit={submitAcoustidRelease}
            onMatchAll={matchQueueToRelease}
          />
        </div>
      )}

      {/* The step the user landed on is the one that answers a missing family:
          say so in place, not only in the banner above. */}
      {missingHere && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          {FAMILY_LABEL[missingHere]} is still open for this album — the import could not decide it.
        </div>
      )}

      {/* ---------------- Step 0: select & separate ---------------- */}
      {step === 0 && (
        <div className="space-y-4">
          <div
            className="panel-hero border-2 border-dashed p-6 sm:p-10 text-center hover:border-accent/60 transition-colors cursor-pointer"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              handleDrop(e.dataTransfer.items);
            }}
            onClick={() => pickFolderBrowser()}
          >
            <UploadCloud className="h-10 w-10 text-zinc-600 mx-auto mb-3" />
            <div className="font-medium text-zinc-300">Drop albums or files here</div>
            <div className="text-xs text-zinc-600 mt-1">
              drop one or more folders (even from different artists) — they are separated into albums below
            </div>
            <div className="text-xs text-zinc-600">
              multi-disc folders (<b className="text-zinc-500">CD1/</b>, <b className="text-zinc-500">Disc 2/</b>) merge into one album
            </div>
            <div className="text-[11px] text-zinc-600 mt-1">
              audio (flac, mp3, m4a, ogg, opus, wav, …) · images (jpg, png, webp, tiff, avif, heic, …) · .lrc .cue .log .accurip
            </div>
            <div className="text-xs text-zinc-600 mt-2">
              click to <b className="text-zinc-400">pick a folder</b> · or{" "}
              <span
                className="text-accent-soft underline underline-offset-2 cursor-pointer"
                onClick={(e) => {
                  e.stopPropagation();
                  document.getElementById("import-files")?.click();
                }}
              >
                browse individual files
              </span>
            </div>
            <input id="import-files" type="file" multiple className="hidden" onChange={(e) => e.target.files && handleFiles(e.target.files)} />
            <input
              id="import-folder"
              type="file"
              multiple
              className="hidden"
              ref={(el) => {
                if (el) {
                  el.setAttribute("webkitdirectory", "");
                  el.setAttribute("directory", "");
                }
              }}
              onChange={(e) => e.target.files && handleFiles(e.target.files)}
            />
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <button className="btn-ghost tap" onClick={pickFolderNative}>
              <FolderOpen className="h-4 w-4" /> Choose folder
            </button>
            <input
              className="input max-w-xs tap"
              placeholder="Default album name"
              value={albumName}
              onChange={(e) => setAlbumName(e.target.value)}
            />
            <label className="flex items-center gap-1.5 text-xs text-zinc-400">
              Media type:
              <select className="input !w-auto !py-1 text-xs tap" value={mediaType} onChange={(e) => setMediaType(e.target.value)}>
                <option value="">Not sure</option>
                <option value="CD">CD rip</option>
                <option value="Digital Media">Digital Media</option>
              </select>
            </label>
            {source === "native" && (
              <span className="text-xs text-zinc-500">importing from disk — files move into your library</span>
            )}
          </div>

          {(mediaType === "" || mediaType === "CD") && totalFiles > 0 && (
            <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
              {mediaType === "CD"
                ? "CD rip: include the .cue, .log and .accurip files alongside the audio so grading and audit can verify the rip."
                : "CD rip? If these files came from a CD, set Media type to “CD rip” and include the .cue and .log files (drop them here or browse them alongside the audio)."}
              {!hasRipFiles && " No .cue/.log files detected in the current selection."}
            </div>
          )}

          {totalFiles > 0 && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold">Separate into albums</span>
                <span className="text-xs text-zinc-500">
                  {totalFiles} file(s) → {albums.length} album(s) — rename, or move files between albums with the dropdown
                </span>
                <button className="btn-ghost !py-1 text-xs ml-auto tap" onClick={addAlbum}>
                  <Plus className="h-3.5 w-3.5" /> Add album
                </button>
              </div>
              {albums.map((g, gi) => (
                <div key={gi} className="panel p-3">
                  <div className="flex flex-wrap items-center gap-2 mb-2">
                    <Disc3 className="h-4 w-4 text-zinc-500 shrink-0" />
                    <input
                      className="input w-full min-w-0 font-medium sm:!w-auto tap"
                      value={g.name}
                      placeholder="Album name"
                      onChange={(e) => renameGroup(gi, e.target.value)}
                    />
                    <span className="text-xs text-zinc-500">
                      {g.files.length - g.files.filter((f) => excluded.has(f.relPath)).length} of {g.files.length} file(s)
                      {g.files.some((f) => excluded.has(f.relPath)) && (
                        <span className="text-amber-300/90"> — partial album</span>
                      )}
                    </span>
                    <button className="btn-danger !px-2 !py-1 ml-auto tap" onClick={() => removeAlbum(gi)} disabled={albums.length <= 1} title="Remove (files move to first album)">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="max-h-52 overflow-auto space-y-1">
                    {g.files.length === 0 && <div className="text-xs text-zinc-600 px-2 py-1">Empty — move files here from other albums.</div>}
                    {g.files.map((f) => (
                      <ImportFileRow
                        key={f.relPath}
                        f={f}
                        gi={gi}
                        albums={albums}
                        groupFiles={g.files}
                        selectable={source === "web"}
                        excluded={excluded.has(f.relPath)}
                        onToggleExcluded={() =>
                          setExcluded((s) => {
                            const next = new Set(s);
                            if (next.has(f.relPath)) next.delete(f.relPath);
                            else next.add(f.relPath);
                            return next;
                          })
                        }
                        onMove={(to) => moveFile(gi, to, f)}
                      />
                    ))}
                    {(() => {
                      const covers = g.files.filter((x) => /\.(jpg|jpeg|png|webp|bmp|gif|tiff|tif|avif|heic|heif|jxl|svg)$/i.test(x.relPath));
                      if (!covers.length) return null;
                      return (
                        <div className="text-[11px] text-zinc-500 px-2 pt-1">
                          {covers.length} image{covers.length === 1 ? "" : "s"} — “cover.jpg” becomes the album cover; an image named like a track (“01 - Song.jpg” next to “01 - Song.flac”) becomes that track's own cover.
                        </div>
                      );
                    })()}
                  </div>
                </div>
              ))}
              <button
                className="btn-primary tap"
                onClick={doImport}
                disabled={uploading || !albums.some((g) => g.name.trim() && g.files.length)}
              >
                {uploading ? "Importing…" : `Import ${albums.filter((g) => g.files.length).length} album(s) into library`}
              </button>
            </div>
          )}
        </div>
      )}

      {/* ---------------- Step 1: links ---------------- */}
      {step === 1 && (
        <div className={minMode && missingHere ? "flex flex-col gap-4" : "space-y-4"}>
          {minMode && !missingHere && <MinNothingMissing />}
          {/* The fingerprint match is how a release is FOUND, not the link
              itself: a minimum visit came for the link, so the match waits
              behind the disclosure. */}
          <MinBlock min={minMode} here={missingHere} mine="">
            {/* Queue mode carries this block in the queue panel above. */}
            {!queueMode && (
              <AcoustidBlock
                match={acoustid}
                busy={acoustidBusy}
                queue={false}
                canMatchAll={false}
                matchAllBusy={false}
                applied={acoustidApplied}
                onRun={runAcoustid}
                onUse={useAcoustidRelease}
                onSubmit={submitAcoustidRelease}
                onMatchAll={matchQueueToRelease}
              />
            )}
          </MinBlock>
          <MinBlock min={minMode} here={missingHere} mine="links">
            <div className="panel p-4 space-y-3">
              <div className="text-sm font-semibold text-zinc-300">
                MusicBrainz release <span className="text-zinc-500 font-normal">— {currentAlbumName}</span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className={`input flex-1 ${releaseId ? "!border-emerald-700" : ""} tap`}
                  placeholder="MusicBrainz release URL or ID (e.g. https://musicbrainz.org/release/…)"
                  value={mbLink}
                  onChange={(e) => setMbLink(e.target.value)}
                />
                {releaseId ? (
                  <span className="chip bg-emerald-900/60 text-emerald-300 border border-emerald-800 shrink-0">
                    <Check className="h-3 w-3" /> {detectedFromTags ? "Detected from track tags" : "Recognized"}
                  </span>
                ) : mbLink.trim() ? (
                  <span className="chip bg-amber-900/50 text-amber-300 border border-amber-900 shrink-0">No MusicBrainz ID found</span>
                ) : null}
              </div>
              {detectStatus !== "idle" && !releaseId && (
                <div className="text-xs text-zinc-500 flex items-center gap-1.5">
                  {detectStatus === "scanning" && (
                    <span className="animate-pulse">Scanning track tags for a MusicBrainz release ID…</span>
                  )}
                  {detectStatus === "none" && !autoNote && (
                    <span>
                      No MusicBrainz release ID found in the track tags — paste a link, search, or{" "}
                      <button className="text-accent-soft underline underline-offset-2" onClick={detectFromTags}>rescan</button>
                    </span>
                  )}
                </div>
              )}
              {/* What the fetch answered when the tags held no ID: the search
                  ran itself when this step opened, so its result is said here
                  rather than left to the user to discover. */}
              {autoNote && detectStatus !== "scanning" && (
                <div className="text-xs text-zinc-400" role="status">
                  {autoNote}
                </div>
              )}
              {detectStatus === "found" && releaseId && (
                <div className="text-xs text-emerald-400 flex items-center gap-1.5">
                  <Check className="h-3 w-3" /> Release detected in track tags — fetched automatically
                </div>
              )}
              <div className="text-xs text-zinc-600">or search:</div>
              <div className="flex gap-2">
                <div className="flex flex-wrap gap-2">
                  <select
                    className="input !w-auto text-xs shrink-0 tap"
                    value={searchMode}
                    onChange={(e) => setSearchMode(e.target.value as any)}
                    title="Search MusicBrainz by"
                  >
                    <option value="release">Title / artist</option>
                    <option value="track">Track title</option>
                    <option value="catno">Catalog number</option>
                    <option value="barcode">Barcode</option>
                  </select>
                  <input className="input tap" placeholder="Search MusicBrainz…" value={mbSearch} onChange={(e) => setMbSearch(e.target.value)} onKeyDown={(e) => e.key === "Enter" && doSearch()} />
                  <button className="btn-ghost shrink-0 tap" onClick={() => doSearch()} disabled={busy}>Search</button>
                </div>
              </div>
              <div className="text-xs text-zinc-600">or find an artist:</div>
              <div className="flex gap-2">
                <input
                  className="input tap"
                  placeholder="Artist name…"
                  value={artistQuery}
                  onChange={(e) => setArtistQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && doArtistSearch()}
                />
                <button className="btn-ghost shrink-0 tap" onClick={doArtistSearch} disabled={busy}>Find artist</button>
              </div>
              {artistHits.length > 0 && (
                <div className="max-h-40 overflow-auto space-y-1">
                  {artistHits.map((a) => (
                    <button key={a.id} className={`${ROW} w-full text-left text-sm hover:bg-raise`}
                      onClick={() => applyArtist(a.name)} title={`Search releases by ${a.name}`}>
                      <span className="flex-1 truncate min-w-0 text-zinc-200">{a.name}</span>
                      {a.type && <span className="chip bg-zinc-800 text-zinc-500 border border-border text-[10px]">{a.type}</span>}
                    </button>
                  ))}
                </div>
              )}
              {searchHits.length > 0 && (
                <div className="max-h-48 overflow-auto space-y-1">
                  {searchHits.map((h) => (
                    <button key={h.id} className={`${ROW} w-full text-left text-sm hover:bg-raise`}
                      onClick={() => { setMbLink(`https://musicbrainz.org/release/${h.id}`); setReleaseId(h.id); setAutoNote(""); }}>
                      <span className="flex-1 truncate min-w-0">
                        <span className="text-zinc-200">{h.title}</span>
                        <span className="text-zinc-500"> — {h.artist} ({h.date})</span>
                      </span>
                      {h.catalog_number && (
                        <span className="chip bg-raise border border-border text-zinc-400 shrink-0">catno {h.catalog_number}</span>
                      )}
                      {h.barcode && (
                        <span className="chip bg-raise border border-border text-zinc-500 font-mono shrink-0">{h.barcode}</span>
                      )}
                      {releaseId === h.id && <Check className="h-4 w-4 text-accent shrink-0" />}
                    </button>
                  ))}
                </div>
              )}
              {release && (
                <div className="text-xs text-zinc-400 pt-2 border-t border-border">
                  <span className="font-semibold text-zinc-200">{release.title}</span> · {release.artists.map((a) => a.name).join(", ")} · {release.date} · {release.medium_count} disc(s) · {release.media.length} tracks
                </div>
              )}
              <div className="text-sm font-semibold text-zinc-300 pt-2">RateYourMusic links (optional)</div>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className={`input flex-1 ${rymValid === true ? "!border-emerald-700" : rymValid === false ? "!border-red-800" : ""} tap`}
                  placeholder="Album: https://rateyourmusic.com/release/…"
                  value={rymLink}
                  onChange={(e) => {
                    setRymLink(e.target.value);
                    setRymNote(""); // a stale "That is an artist page" must not outlive the paste it described
                  }}
                />
                <LinkValidChip state={rymValid} kind={rymKind} />
                <button
                  className="btn-ghost shrink-0 tap"
                  onClick={findRymLinks}
                  disabled={findingLinks || busy}
                  title="Ask RateYourMusic for this album's and this artist's pages and fill both fields for review"
                >
                  {findingLinks ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}{" "}
                  Find links
                </button>
              </div>
              {rymNote && <div className="text-[10px] text-amber-300/80">{rymNote}</div>}
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className={`input flex-1 ${rymArtistValid === true ? "!border-emerald-700" : rymArtistValid === false ? "!border-red-800" : ""} tap`}
                  placeholder="Artist: https://rateyourmusic.com/artist/…"
                  value={rymArtistLink}
                  onChange={(e) => {
                    setRymArtistLink(e.target.value);
                    setRymArtistNote("");
                  }}
                />
                <LinkValidChip state={rymArtistValid} kind={rymArtistKind} />
              </div>
              <div className="text-[10px] text-zinc-600">
                Written to every track as RATEYOURMUSIC_ARTIST — only an artist page is accepted.
              </div>
              {rymArtistNote && <div className="text-[10px] text-amber-300/80">{rymArtistNote}</div>}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button className="btn-primary tap" onClick={handleFetch} disabled={busy}>
                <Wand2 className="h-4 w-4" /> Fetch release & auto-match
              </button>
              <button className="btn-ghost tap" onClick={detectFromTags} disabled={busy || !albumPath}>
                Detect from tags
              </button>
              {busy && fetchStatus && (
                <span className="text-xs text-accent-soft animate-pulse flex items-center gap-1.5">
                  {fetchStatus}
                </span>
              )}
            </div>
          </MinBlock>
        </div>
      )}

      {/* ---------------- Step 2: matching ---------------- */}
      {step === 2 && (
        <div className="space-y-4">
          <div className="text-sm text-zinc-400">
            Confirm each local track's MusicBrainz track/disc. Unmatched rows stay blank — you can also fix them manually later on the track page.
          </div>
          {suggestions.length === 0 && <div className="text-xs text-zinc-500">No data — go back and fetch the release.</div>}
          {groupByDisc(suggestions, discOfSuggestion).map((g) => (
            <DiscSection
              key={g.disc ?? "unmatched"}
              disc={g.disc}
              count={g.rows.length}
              collapsed={collapsedDiscs.has(g.disc ?? null)}
              onToggle={() => toggleDisc(g.disc ?? null)}
            >
              {g.rows.map((s) => {
                return (
                  <div key={s.local} className={ROW_WRAP}>
                    <TrackNoBadge disc={discNoOf(s.local)} track={trackNoOf(s.local)} />
                    <span className="flex-1 truncate text-sm">{displayTitle(s.local)}</span>
                    <span className="min-w-0 truncate text-xs text-zinc-500">
                      {s.matched ? `${s.release_track!.disc}.${s.release_track!.position} ${s.release_track!.title}` : "no match"}
                    </span>
                    <select
                      className="input !w-auto !py-1 text-xs max-w-full tap"
                      value={s.release_track ? `${s.release_track.disc}-${s.release_track.position}` : ""}
                      onChange={(e) => {
                        const [d, p] = e.target.value.split("-").map(Number);
                        setSuggestion(s.local, d, p);
                      }}
                    >
                      <option value="">— none —</option>
                      {release?.media.map((m) => (
                        <option key={`${m.disc}-${m.position}`} value={`${m.disc}-${m.position}`}>
                          {m.disc}.{m.position} {m.title}
                        </option>
                      ))}
                    </select>
                  </div>
                );
              })}
            </DiscSection>
          ))}
          <div className="flex justify-end">
            <button className="btn-primary tap" onClick={confirmMatch} disabled={busy}>
              Save matching
            </button>
          </div>
        </div>
      )}

      {/* ---------------- Step 3: covers ---------------- */}
      {step === 3 && albumPath && (
        <div className={minMode && missingHere ? "flex flex-col gap-4" : "space-y-4"}>
          {minMode && !missingHere && <MinNothingMissing />}
          <MinBlock min={minMode} here={missingHere} mine="cover">
            {coverNotice && (
              <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
                {coverNotice}
              </div>
            )}
          </MinBlock>

          <MinBlock min={minMode} here={missingHere} mine="">
            {/* What an import owes besides the cover: the artist's image and
                description, and the album's own description. Each row states
                whether it is already there and who supplied it; one button
                fetches whatever is missing and the rows are re-read after. */}
            <div className="panel px-3 py-2 space-y-1.5">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-semibold text-zinc-300">Artist &amp; album metadata</span>
                <span className="text-[11px] text-zinc-500">
                  artist: {artistName || "—"}
                  {artistArt?.path ? <span className="font-mono"> · {artistArt.path}</span> : null}
                </span>
                {metaReply && (
                  <button className="btn-ghost !py-0.5 !px-1.5 text-[11px] ml-auto tap" onClick={() => setMetaReply(null)}>
                    Clear result
                  </button>
                )}
                <button
                  className={`btn-ghost !py-1 text-xs tap ${metaReply ? "" : "ml-auto"}`}
                  onClick={fetchArtistMeta}
                  disabled={busy || !albumPath}
                  title="Ask the configured sources for the missing artist image, artist description and album description; what is already present is left alone"
                >
                  <CloudDownloadIcon /> Fetch missing
                </button>
              </div>
              {act?.kind === "metadata" && (
                <ActionBar active label={act.label} done={act.done} total={act.total} />
              )}
              {metaRows.map((row) => {
                const item = metaReply?.[row.kind];
                return (
                  <div key={row.kind} className="flex flex-wrap items-center gap-2 text-[11px]">
                    <span className="w-28 sm:w-40 shrink-0 text-zinc-400">{row.label}</span>
                    <span
                      className={`chip border shrink-0 ${
                        row.present
                          ? "bg-emerald-900/40 text-emerald-300 border-emerald-800"
                          : "bg-raise text-zinc-500 border-border"
                      }`}
                    >
                      {row.present ? "present" : "missing"}
                    </span>
                    <span className="text-zinc-500 truncate" title={row.source ?? undefined}>
                      {row.present ? (row.source ?? "source unknown") : ""}
                    </span>
                    {!row.enabled && (
                      <span className="text-amber-300/90 shrink-0">
                        fetching switched off in Settings (Artist images &amp; descriptions)
                      </span>
                    )}
                    {item && (
                      <span
                        className={`ml-auto shrink-0 ${item.state === "error" ? "text-red-300" : "text-zinc-400"}`}
                        title={item.detail ?? undefined}
                      >
                        {item.state === "fetched" ? `fetched · ${item.source ?? "?"}` : `${item.state}${item.detail ? ` — ${item.detail}` : ""}`}
                      </span>
                    )}
                  </div>
                );
              })}
              {metaError && (
                <div className="text-[11px] text-red-300" role="alert">
                  Metadata fetch failed — {metaError}
                </div>
              )}
            </div>
          </MinBlock>

          <MinBlock min={minMode} here={missingHere} mine="cover">
            <div className="grid md:grid-cols-2 gap-3">
              <div className="panel p-4 space-y-2">
                <div className="text-sm font-semibold text-zinc-300">Current album cover</div>
                <CoverImg
                  albumPath={albumPath}
                  coverFile={coverInfo?.file}
                  staged={staged}
                  wrapperClass="h-40 w-40 rounded-lg bg-raise border border-border overflow-hidden"
                />
                <div className="text-xs text-zinc-500">
                  {coverInfo?.file
                    ? `${coverInfo.file} — ${coverInfo.width ?? "?"}×${coverInfo.height ?? "?"} px · ${Math.max(1, Math.round(coverInfo.bytes / 1024))} KB`
                    : "No album cover on disk yet."}
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <button className="btn-ghost !py-1 text-xs tap" onClick={() => albumCoverInput.current?.click()} disabled={busy}>
                    <UploadCloud className="h-3.5 w-3.5" /> Upload image
                  </button>
                  <button className="btn-ghost !py-1 text-xs tap" onClick={() => setCoverSearch({})} disabled={busy}>
                    Search covers
                  </button>
                </div>
                {/* The import staged covers for this album but the pick is the
                    user's — same one-click affordance as the album page. */}
                {!coverInfo?.file && !!stagedCoverRows?.length && (
                  <>
                    <button
                      className="btn-primary !py-1.5 text-xs tap"
                      onClick={() =>
                        setCoverSearch({ results: stagedCoverRows, provider: stagedCovers.data?.provider ?? null })
                      }
                      title="Covers fetched during import, ranked by the cover policy, waiting for you to pick one"
                    >
                      <ImageIcon className="h-3.5 w-3.5" /> Choose a cover ({stagedCoverRows.length})
                    </button>
                    {/* the policy's own pick, said before the picker opens:
                        what it is, and the reason that put it first */}
                    {stagedCovers.data?.chosen && (
                      <div className="text-[11px] text-zinc-500">
                        {t("cover.best_pick")}:{" "}
                        <span className="text-zinc-300">
                          {stagedCovers.data.chosen.source}
                          {stagedCovers.data.chosen.width && stagedCovers.data.chosen.height
                            ? ` · ${stagedCovers.data.chosen.width}×${stagedCovers.data.chosen.height}px`
                            : ""}
                        </span>
                        <span className="block text-zinc-600">
                          {t("cover.pick_reason")}:{" "}
                          {(stagedCovers.data.chosen.reasons ?? []).slice(-1)[0] ?? ""}
                        </span>
                      </div>
                    )}
                  </>
                )}
              </div>

              <div className="panel p-4 space-y-2">
                <div className="text-sm font-semibold text-zinc-300">MusicBrainz release cover</div>
                <div className="text-xs text-zinc-500">
                  The release's own cover — compare it with musichoarders before you accept it.
                </div>
                {mbCoverUrl ? (
                  <img
                    src={api.artUrl(mbCoverUrl)}
                    alt="MusicBrainz release cover"
                    referrerPolicy="no-referrer"
                    className="h-40 w-40 rounded-lg bg-raise border border-border object-cover"
                  />
                ) : (
                  <div className="h-40 w-40 rounded-lg bg-raise border border-border flex items-center justify-center text-center px-3 text-xs text-zinc-600">
                    No release picked yet — fetch it in the Links step.
                  </div>
                )}
                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    className="btn-ghost !py-1 text-xs tap"
                    onClick={() => mbCoverUrl && applyCoverUrl(mbCoverUrl)}
                    disabled={busy || !mbCoverUrl}
                  >
                    <CloudDownloadIcon /> Use the MusicBrainz cover
                  </button>
                  {mbCoverUrl && (
                    <a
                      href={mbCoverUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs text-accent-soft underline underline-offset-2"
                    >
                      open on coverartarchive.org
                    </a>
                  )}
                </div>
              </div>
            </div>

            <div className="panel p-3 space-y-2">
              <div className="text-xs text-zinc-400">
                MusicBrainz cover wrong? Find the correct one on{" "}
                <a
                  href="https://covers.musichoarders.xyz"
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent-soft underline underline-offset-2"
                >
                  covers.musichoarders.xyz <ExternalLink className="inline h-3 w-3" />
                </a>
                , then apply it here with Search covers or an image URL.
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-semibold text-zinc-400">Album cover from URL</span>
                <input
                  className="input flex-1 min-w-0 !py-1 text-xs tap"
                  placeholder="https://…/cover.jpg"
                  value={coverUrl}
                  onChange={(e) => setCoverUrl(e.target.value)}
                />
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => applyCoverUrl(coverUrl)}
                  disabled={busy || !coverUrl.trim()}
                >
                  <ExternalLink className="h-3.5 w-3.5" /> Apply to album
                </button>
              </div>
            </div>

            <div className="panel p-3 space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-semibold text-zinc-300">Per-track covers</span>
                <span className="text-xs text-zinc-500">
                  {coverSel.size} selected — one image applied to several tracks is stored once and shared between them (that is how
                  tracks 7 and 8 get the same art).
                </span>
                <button
                  className="btn-ghost !py-1 text-xs ml-auto tap"
                  onClick={() =>
                    setCoverSel(
                      coverSel.size && coverSel.size === stepTracks.length
                        ? new Set()
                        : new Set(stepTracks.map((t) => t.path))
                    )
                  }
                >
                  {coverSel.size && coverSel.size === stepTracks.length ? "Select none" : "Select all"}
                </button>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => trackCoverInput.current?.click()}
                  disabled={busy || !coverSel.size}
                >
                  <UploadCloud className="h-3.5 w-3.5" /> Upload to selected
                </button>
                <input
                  className="input !w-64 !py-1 text-xs tap"
                  placeholder="Cover image URL for the selection…"
                  value={trackCoverUrl}
                  onChange={(e) => setTrackCoverUrl(e.target.value)}
                />
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => applyCoverUrl(trackCoverUrl, selectedCoverFiles())}
                  disabled={busy || !coverSel.size || !trackCoverUrl.trim()}
                >
                  <ExternalLink className="h-3.5 w-3.5" /> Use URL
                </button>
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => mbCoverUrl && applyCoverUrl(mbCoverUrl, selectedCoverFiles())}
                  disabled={busy || !coverSel.size || !mbCoverUrl}
                >
                  <CloudDownloadIcon /> MusicBrainz cover
                </button>
                <button className="btn-danger !py-1 text-xs tap" onClick={clearTrackCovers} disabled={busy || !coverSel.size}>
                  <Trash2 className="h-3.5 w-3.5" /> Clear per-track cover
                </button>
              </div>
              {stepTracks.length === 0 && (
                <div className="text-xs text-zinc-500">No tracks — go back and fetch the release.</div>
              )}
              {groupByDisc(stepTracks, discOfTrack).map((g) => (
                <DiscSection
                  key={g.disc ?? "unmatched"}
                  disc={g.disc}
                  count={g.rows.length}
                  collapsed={collapsedDiscs.has(g.disc ?? null)}
                  onToggle={() => toggleDisc(g.disc ?? null)}
                  extra={
                    <>
                      <button
                        className="btn-ghost !py-0.5 !px-1.5 text-[11px] tap"
                        onClick={() => setCoverSelFor(g.rows.map((t) => t.path), true)}
                      >
                        All
                      </button>
                      <button
                        className="btn-ghost !py-0.5 !px-1.5 text-[11px] tap"
                        onClick={() => setCoverSelFor(g.rows.map((t) => t.path), false)}
                      >
                        None
                      </button>
                    </>
                  }
                >
                  {g.rows.map((t) => {
                    const shared = t.cover_file ? (coverShares.get(t.cover_file) ?? 0) - 1 : 0;
                    return (
                      <label
                        key={t.path}
                        className={`${ROW} cursor-pointer ${
                          coverSel.has(t.path) ? "border-accent/60" : "border-border"
                        }`}
                      >
                        <input type="checkbox" checked={coverSel.has(t.path)} onChange={() => toggleCoverSel(t.path)} />
                        <TrackCover albumPath={albumPath} trackCover={t.cover_file} albumCover={coverInfo?.file} staged={staged} />
                        <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
                        <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
                        {t.cover_file ? (
                          <span
                            className="chip bg-accent/10 text-accent-soft border border-accent/25 shrink-0"
                            title={t.cover_file}
                          >
                            own art{shared > 0 ? ` · shared with ${shared}` : ""}
                          </span>
                        ) : (
                          <span className="chip bg-raise border border-border text-zinc-500 shrink-0">album cover</span>
                        )}
                      </label>
                    );
                  })}
                </DiscSection>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2 justify-end">
              <span className="text-xs text-zinc-500">Covers are written as you apply them — Continue just moves on.</span>
              <button className="btn-primary tap" onClick={saveCovers}>Continue to genres</button>
            </div>

            <input
              ref={albumCoverInput}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.target.value = "";
                if (f) uploadCover(f);
              }}
            />
            <input
              ref={trackCoverInput}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                const files = selectedCoverFiles();
                e.target.value = "";
                if (f && files.length) uploadCover(f, files);
              }}
            />

            {coverSearch && (
              <CoverSearchModal
                albumPath={albumPath}
                artist={release?.artists.map((a) => a.name).join(", ") || trackArtist(stepTracks[0]?.path ?? "")}
                album={trackAlbum || currentAlbumName}
                releaseGroupMbid={release?.release_group_id ?? undefined}
                releaseMbid={releaseId || undefined}
                tracks={coverSel.size ? selectedCoverFiles() : undefined}
                // The release this album is being matched to states its own
                // tracklist, so the search verifies a candidate's release
                // against it: a karaoke or other-album row can carry the same
                // artist and title, and only the count tells them apart. The
                // folder's own file count is NOT used — a partial rip of the
                // matched release would then contradict the right cover.
                trackCount={release?.media?.length || undefined}
                initialResults={coverSearch.results}
                initialProvider={coverSearch.provider}
                initialChosen={stagedCovers.data?.chosen ?? null}
                initialNotes={stagedCovers.data?.notes ?? []}
                onClose={() => setCoverSearch(null)}
                onApplied={() => {
                  refreshCovers();
                  qc.invalidateQueries({ queryKey: ["stagedCovers", albumPath] });
                  qc.invalidateQueries({ queryKey: ["album"] });
                }}
              />
            )}
          </MinBlock>
        </div>
      )}

      {/* ---------------- Step 4: genres ---------------- */}
      {step === 4 && (
        <div className={minMode && missingHere ? "flex flex-col gap-4" : "space-y-4"}>
          {minMode && !missingHere && <MinNothingMissing />}
          <MinBlock min={minMode} here={missingHere} mine="">
            <div className="flex items-center gap-2 flex-wrap">
              <button
                className="btn-ghost tap"
                onClick={() => importGenresFrom("musicbrainz")}
                disabled={busy || !albumTargets().length}
                title="Ask MusicBrainz for this album's genres (recording → release → release group → artist) and write what it states"
              >
                <CloudDownloadIcon /> Genres from MusicBrainz
              </button>
              <button
                className="btn-ghost tap"
                onClick={() => importGenresFrom("rateyourmusic")}
                disabled={busy || !albumTargets().length}
                title="Ask RateYourMusic for this album's genres and write what its page states — a blocked RYM says so instead of writing a guess"
              >
                <CloudDownloadIcon /> Genres from RateYourMusic
              </button>
              {/* The per-run limit can only LOWER the app's own cap: the server
                  writes and trims every genre list to `mb_genre_count`, so an
                  option above it would be a promise nothing keeps. */}
              <label className="flex items-center gap-1.5 text-xs text-zinc-400 ml-auto">
                Max genres / track
                <select
                  className="input !w-auto !py-1 text-xs tap"
                  value={genreLimitValue}
                  onChange={(e) => setGenreLimit(Number(e.target.value))}
                  title={`The app writes at most ${genreCap} genre value(s) per track (mb_genre_count, Settings → Import)`}
                >
                  {Array.from({ length: genreCap }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={n}>
                      {n}
                      {n === genreCap ? " (app cap — Settings → Import)" : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {/* What the last source wrote — one source at a time, so a blocked
                or empty one is never hidden behind the other's answer. */}
            {genreError && (
              <div className="text-xs text-red-300" role="alert">
                {genreError}
              </div>
            )}
            {genreJobResult && (
              <div className="panel px-3 py-2 space-y-1" role="status">
                <div className="text-xs text-zinc-300">
                  {genreJobResult.updated} track(s) updated
                  {jobGenres.length ? "" : " — no genres returned"}
                </div>
                {/* What the run wrote, one chip per name with the family first
                    — the order the write puts it in. This printed the union as
                    one comma-joined line in the sources' own spelling, so
                    three genres read as one name. */}
                {jobGenres.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5">
                    {jobGenres.map((g) => (
                      <span
                        key={g}
                        className={
                          g === jobFamily
                            ? "chip bg-raise border border-border text-zinc-400"
                            : "chip bg-accent/10 text-accent-soft border border-accent/25"
                        }
                      >
                        {g === jobFamily && (
                          <span className="text-[9px] uppercase tracking-wider text-zinc-600">family</span>
                        )}
                        {g}
                      </span>
                    ))}
                  </div>
                )}
                {/* The cap these genres were written and trimmed to, so the
                    value is visible where the genres are, not only in Settings
                    → Import (`mb_genre_count`). */}
                <div className="text-[11px] text-zinc-500">
                  Genres per track: {genreJobResult.genre_count} (Settings → Import)
                  {genreJobResult.trimmed > 0 &&
                    ` — ${genreJobResult.trimmed} track(s) trimmed to ${genreJobResult.genre_count}`}
                </div>
                {/* What each source answered, its own names beside it — the
                    count says how many, the names were a `title` only and so
                    unreadable on the page. */}
                {genreSourceRows.length > 0 && (
                  <div className="space-y-1">
                    {genreSourceRows.map(([name, names]) => (
                      <div key={name} className="flex flex-wrap items-center gap-1.5">
                        <span className="chip bg-raise border border-border text-zinc-400 shrink-0">
                          {name} <span className="tabular-nums">{names.length}</span>
                        </span>
                        {names.map((n) => (
                          <span key={n} className="chip bg-raise border border-border text-zinc-300">
                            {n}
                          </span>
                        ))}
                      </div>
                    ))}
                  </div>
                )}
                {Object.entries(genreJobResult.notes).length > 0 && (
                  <ul className="text-[11px] text-zinc-500 space-y-0.5">
                    {Object.entries(genreJobResult.notes).map(([name, note]) => (
                      <li key={name}>
                        <span className="text-zinc-400">{name}</span>: {note}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            <span className="text-xs text-zinc-500 -mt-1 block">
              Genres are not fetched automatically — set the per-track limit, then ask MusicBrainz, RateYourMusic, or both.
              The app derives the family (rock, electronic…) from the specific genres and writes it in the first slot,
              the specific genres after it: at most {genreCap} genre value{genreCap === 1 ? "" : "s"} per track (Settings → Import).
            </span>
          </MinBlock>
          <MinBlock min={minMode} here={missingHere} mine="genres">
            {/* One datalist for every add input in the step: the browser's own
                autocomplete, so a name is offered the way MusicBrainz spells it
                without a keystroke of React work — and the options stay one DOM
                copy however many tracks the album has. */}
            <datalist id="wizard-genres">
              {[...genreSuggestions.values()].map((n) => (
                <option key={n} value={n} />
              ))}
            </datalist>
            {stepTracks.length === 0 && <div className="text-xs text-zinc-500">No tracks — go back and fetch the release.</div>}
            {/* Album-wide cleanup: every genre currently on any track, one click
                to strip it from ALL of them, plus a clear-everything button.
                A wrong genre lands on a whole album at once, so removing it
                everywhere is the common correction — and until now it could only
                be done one track at a time. */}
            {allGenres.length > 0 && (
              <div className="panel px-3 py-2 flex items-center gap-1.5 flex-wrap">
                <span className="text-xs font-semibold text-zinc-400 shrink-0">Remove a genre everywhere:</span>
                {allGenres.map(([gen, n]) => (
                  <button
                    key={gen}
                    className={`chip border ${GENRE_FAMILIES[gen.toLowerCase()]
                      ? "bg-raise border-border text-zinc-500"
                      : "bg-raise border-border text-zinc-300"} hover:border-red-800 hover:text-red-200`}
                    onClick={() => removeGenreEverywhere(gen)}
                    title={GENRE_FAMILIES[gen.toLowerCase()]
                      // A derived family comes back on the next write, so this
                      // only helps while the step is open — say so.
                      ? `Remove “${gen}” (a derived family) from all ${n} track(s) for this run`
                      : `Remove “${gen}” from all ${n} track(s)`}
                  >
                    {gen}
                    <span className="text-[10px] text-zinc-500 tabular-nums">{n}</span>
                    <X className="h-3 w-3" />
                  </button>
                ))}
                <button
                  className="btn-danger !py-1 text-xs ml-auto tap"
                  onClick={removeAllGenres}
                  title="Clear the genre field on every track in this step"
                >
                  <Trash2 className="h-3.5 w-3.5" /> Remove all genres
                </button>
              </div>
            )}
            {groupByDisc(stepTracks, discOfTrack).map((g) => (
              <DiscSection
                key={g.disc ?? "unmatched"}
                disc={g.disc}
                count={g.rows.length}
                collapsed={collapsedDiscs.has(g.disc ?? null)}
                onToggle={() => toggleDisc(g.disc ?? null)}
                extra={
                  g.disc
                    ? [
                        <input
                          key="in"
                          className="input !w-52 !py-1 text-xs tap"
                          list="wizard-genres"
                          placeholder="Apply genre to whole disc…"
                          value={discGenres[g.disc!] ?? ""}
                          onChange={(e) => setDiscGenres((m) => ({ ...m, [g.disc!]: e.target.value }))}
                          onKeyDown={(e) => e.key === "Enter" && applyGenresToDisc(g.disc!, (discGenres[g.disc!] ?? "").trim())}
                        />,
                        <button
                          key="btn"
                          className="btn-ghost !py-1 text-xs tap"
                          onClick={() => applyGenresToDisc(g.disc!, (discGenres[g.disc!] ?? "").trim())}
                        >
                          Apply to all
                        </button>,
                      ]
                    : undefined
                }
              >
                {g.rows.map((t) => {
                  const list = genreList(t.path);
                  const family = familyOf(list);
                  const typed = genreAddValues[t.path] ?? "";
                  const unknown = !!typed.trim() && !genreSuggestions.has(typed.trim().toLowerCase());
                  return (
                    <div key={t.path} className={ROW}>
                      <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
                      <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
                      <div className="flex items-center gap-1.5 flex-wrap justify-end">
                        {family ? (
                          // The family's own slot: it is DERIVED from the
                          // specific genre and written FIRST — the slot the
                          // grader's GENRE_ORDER check reads — so it is
                          // labelled and rendered first rather than shown as
                          // one more name to edit.
                          <span
                            className="chip bg-raise border border-border text-zinc-400"
                            title={`${family} is the family — the app derives it from the specific genre and writes it in the first slot`}
                          >
                            <span className="text-[9px] uppercase tracking-wider text-zinc-600">family</span>
                            {family}
                            <button
                              className="hover:text-white transition-colors"
                              onClick={() => removeGenre(t.path, family)}
                              title={`Remove ${family} for this run — the app derives it again when it writes`}
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </span>
                        ) : (
                          list.length > 0 && (
                            <span
                              className="chip bg-raise border border-dashed border-border text-zinc-600"
                              title={`The app derives the family of ${list[0]} when it writes — it is not typed in here`}
                            >
                              family derived on save
                            </span>
                          )
                        )}
                        {list.filter((x) => x !== family).map((gen) => (
                          <span key={gen} className="chip bg-accent/10 text-accent-soft border border-accent/25">
                            {gen}
                            <button
                              className="hover:text-white transition-colors"
                              onClick={() => removeGenre(t.path, gen)}
                              title={`Remove ${gen}`}
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </span>
                        ))}
                        <input
                          className={`input !w-36 !py-1 text-xs tap${unknown ? " !border-amber-700" : ""}`}
                          list="wizard-genres"
                          placeholder={list.length ? "+ add genre…" : "Add genre…"}
                          value={typed}
                          disabled={list.length >= genreCap}
                          title={
                            list.length >= genreCap
                              ? `Genres per track is ${genreCap} (Settings → Import) — remove one to add another`
                              : unknown
                                ? `“${typed.trim()}” is not a name the app's vocabulary knows — it is written as typed and the grade check flags it`
                                : undefined
                          }
                          onChange={(e) => setGenreAddValues((v) => ({ ...v, [t.path]: e.target.value }))}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              addGenre(t.path, typed);
                              setGenreAddValues((v) => ({ ...v, [t.path]: "" }));
                            }
                          }}
                        />
                      </div>
                    </div>
                  );
                })}
              </DiscSection>
            ))}
            <div className="flex justify-end">
              <button className="btn-primary tap" onClick={saveGenres} disabled={busy}>Save genres</button>
            </div>
          </MinBlock>
        </div>
      )}

      {/* ---------------- Step 5: lyrics ---------------- */}
      {step === 5 && (
        <div className={minMode && missingHere ? "flex flex-col gap-4" : "space-y-4"}>
          {minMode && !missingHere && <MinNothingMissing />}
          <MinBlock min={minMode} here={missingHere} mine="lyrics">
            {lyricsNotice && (
              <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
                {lyricsNotice}
              </div>
            )}
          </MinBlock>
          <MinBlock min={minMode} here={missingHere} mine="">
            {/* The lyrics family's option group: one option per half of the
                chain an import runs (script 13 → 17 → 18), so nothing the
                chain decides is unreachable by hand. Each button calls the
                half's own entry point — the fetch is script 13's engine, the
                transliteration pass is script 17's runner, the publish is
                script 18's per-track core — and reports what that pass did. */}
            <div className="panel px-3 py-2 space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <button className="btn-primary text-xs tap" onClick={() => autoImportLyrics()} disabled={busy}>
                  <CloudDownloadIcon /> Auto-import lyrics
                </button>
                <span className="text-xs text-zinc-500">
                  Tries every provider in the saved order (Settings → Lyrics) and writes them straight to the
                  files. Review below — Space stamps time while previewing; INSTRUMENTAL=1 skips lyrics.
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => runLyricsPass("xlit")}
                  disabled={busy || !stepTracks.length}
                  title="Script 17 on this album's tracks: write the TRANSLITERATION-<lang> / TRANSLATION-<lang> tags the lyrics need (and the .romaji.lrc / .<lang>.lrc sidecars for the LRC formats). Needs AI configured in Settings → AI."
                >
                  <Languages className="h-3.5 w-3.5" /> Transliterate / translate lyrics
                </button>
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => runLyricsPass("publish")}
                  disabled={busy || !stepTracks.length}
                  title="Script 18 on this album's tracks: submit the lyrics LRCLIB does not have yet. Nothing is written to the files — this is the one outward lyrics step."
                >
                  <UploadCloud className="h-3.5 w-3.5" /> Publish lyrics to LRCLIB
                </button>
                <span className="text-[11px] text-zinc-500">
                  The chain's other two lyrics steps, by hand: what LRCLIB lacks in the database is what
                  publishing gives it, and both run over the same {stepTracks.length} track(s).
                </span>
              </div>
              {lyrPass && (
                <div
                  className={`text-[11px] ${lyrPass.failed ? "text-red-300" : "text-zinc-400"}`}
                  role={lyrPass.failed ? "alert" : undefined}
                >
                  {lyrPass.kind === "xlit" ? "Transliteration" : "LRCLIB publish"} — {lyrPass.text}
                </div>
              )}
            </div>
            {Object.keys(lyrResults).length > 0 && (
              <div className="panel px-3 py-2 text-xs space-y-0.5">
                <div className="flex items-center gap-3 text-zinc-400 flex-wrap">
                  {Object.entries(LYR_STATUS_LABEL).map(([status, label]) => {
                    const rows = Object.values(lyrResults).filter((r) => r.status === status);
                    if (!rows.length) return null;
                    const labels = [...new Set(rows.map((r) => r.provider_label).filter(Boolean))];
                    return (
                      <span key={status}>
                        <b className="text-zinc-200">{rows.length}</b> {label}
                        {status === "ok" && labels.length > 0 && ` — ${labels.join(", ")}`}
                      </span>
                    );
                  })}
                </div>
                <div className="text-[10px] text-zinc-600">
                  Per track below — nothing found is normal for instrumentals and unreleased tracks.
                </div>
              </div>
            )}
          </MinBlock>
          <MinBlock min={minMode} here={missingHere} mine="lyrics">
            {/* One compact row per track, like every other step: badge, title and
                the chips on a single line, the editor behind Edit. */}
            {stepTracks.map((t) => {
              const inst = instrumental[t.path] ?? t.tags.INSTRUMENTAL;
              const hasDraft = hasLyrics(t);
              const open = lyrOpen.has(t.path);
              return (
                <div key={t.path} className={`${ROW} flex-col items-stretch space-y-2`}>
                  <div className="flex flex-wrap items-center gap-3">
                    <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
                    <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
                    {hasDraft && (
                      <span className="chip bg-emerald-900/60 text-emerald-300 border border-emerald-800 shrink-0">
                        <Check className="h-3 w-3" /> Lyrics
                      </span>
                    )}
                    {lyrResults[t.path] && (
                      <span
                        className={`chip border shrink-0 ${
                          lyrResults[t.path].status === "ok"
                            ? "bg-emerald-900/50 text-emerald-300 border-emerald-800"
                            : lyrResults[t.path].status === "failed"
                              ? "bg-red-900/40 text-red-300 border-red-900"
                              : "bg-raise text-zinc-500 border-border"
                        }`}
                        title={lyrResults[t.path].reason || lyrResults[t.path].error || ""}
                      >
                        {lyrResults[t.path].status === "ok"
                          ? lyrResults[t.path].provider_label
                          : LYR_STATUS_LABEL[lyrResults[t.path].status] ?? lyrResults[t.path].status}
                      </span>
                    )}
                    {inst === "1" && (
                      <span className="chip bg-raise text-zinc-400 border border-border shrink-0">Instrumental</span>
                    )}
                    <label className="flex items-center gap-1.5 text-xs text-zinc-400 select-none shrink-0">
                      <input
                        type="checkbox"
                        checked={inst === "1"}
                        onChange={(e) => setInstrumental((m) => ({ ...m, [t.path]: e.target.checked ? "1" : "0" }))}
                        className=""
                      />
                      INSTRUMENTAL
                    </label>
                    <button
                      className="btn-ghost !py-1 text-xs shrink-0 tap"
                      onClick={() => toggleLyricsRow(t.path)}
                      disabled={inst === "1"}
                      title={inst === "1" ? "Marked instrumental — uncheck INSTRUMENTAL to edit lyrics" : "Open the lyrics editor for this track"}
                    >
                      {open ? "Hide" : "Edit"}
                    </button>
                  </div>
                  {open && inst !== "1" && (
                    <div className="space-y-1.5">
                      <button
                        className="btn-ghost !py-0.5 text-[11px] tap"
                        onClick={() => autoImportLyrics([t.path])}
                        disabled={busy}
                        title="Fetch this track's lyrics through the provider chain and write them to the file"
                      >
                        <CloudDownloadIcon /> Auto-import lyrics
                      </button>
                      <LyricsViewer
                        path={t.path}
                        initialLyrics={lyricsDrafts[t.path] ?? ""}
                        onChange={(lrc) => setLyricsDrafts((d) => ({ ...d, [t.path]: lrc }))}
                        artist={trackArtist(t.path)}
                        track={trackTitle(t.path)}
                        album={trackAlbum}
                        duration={trackDuration(t.path)}
                        staged={staged}
                      />
                    </div>
                  )}
                </div>
              );
            })}
            <div className="flex justify-end">
              <button className="btn-primary tap" onClick={saveLyricsStep} disabled={busy}>Save lyrics & instrumental</button>
            </div>
          </MinBlock>
        </div>
      )}

      {/* ---------------- Step 6: advisory ---------------- */}
      {step === 6 && (
        <div className={minMode && missingHere ? "flex flex-col gap-4" : "space-y-4"}>
          {minMode && !missingHere && <MinNothingMissing />}
          <MinBlock min={minMode} here={missingHere} mine="">
            <div className="text-sm text-zinc-400">Set iTunes advisory per track: <b className="text-zinc-200">0</b> unrated/clean, <b className="text-zinc-200">1</b> explicit, <b className="text-zinc-200">2</b> safe edited version.</div>
            <div className="flex items-center gap-2 panel px-3 py-2 flex-wrap">
              <span className="text-xs font-semibold text-zinc-400">Apply to all tracks:</span>
              {["0", "1", "2"].map((v) => (
                <button
                  key={v}
                  onClick={() => applyAdvisoryToAll(v)}
                  className="btn-ghost !py-1 text-xs tap"
                  title={`Set every track to ${v === "0" ? "clean" : v === "1" ? "explicit" : "safe"}`}
                >
                  {v === "0" ? "0 · clean" : v === "1" ? "1 · explicit" : "2 · safe"}
                </button>
              ))}
            </div>
            {/* Fetch the rating for the WHOLE album here: the wizard used to
                have no way to run the advisory sources at all — this is the
                album page's own Check, with the bar and the per-track outcome
                the click deserves. */}
            <div className="panel px-3 py-2 space-y-1.5">
              <div className="flex items-center gap-2 flex-wrap">
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => fetchAdvisoryAll(false)}
                  disabled={busy || !stepTracks.length}
                  title="Ask the configured advisory sources (Deezer / Spotify by ISRC, Apple) for every track and write what they state. A track that already carries a value keeps it — use Re-rate to ask anyway."
                >
                  <CloudDownloadIcon /> Auto-import advisory for all tracks
                </button>
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={reRateAdvisoryAll}
                  disabled={busy || !stepTracks.length}
                  title="Ask the sources again even for tracks that already carry a value, and write what they state — the only way a rating can go down"
                >
                  <RotateCcw className="h-3 w-3" /> Re-rate…
                </button>
                <span className="text-[11px] text-zinc-500">
                  Asks the same sources the album page's Check does, for all {stepTracks.length} track(s) at once.
                  {advReply ? ` ${advisoryOutcome(advReply)}` : ""}
                </span>
              </div>
              {advError && (
                <div className="text-xs text-red-300" role="alert">
                  Advisory fetch failed — {advError}
                </div>
              )}
              {advReply && (
                <div className="space-y-0.5">
                  {stepTracks.map((t) => (
                    <div key={t.path} className="flex items-center gap-2 text-[11px]">
                      <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
                      <span className="flex-1 truncate text-zinc-400">{displayTitle(t.path)}</span>
                      {/* The line carries what happened to THIS track's value
                          (`status`): a re-check the sources agreed with and a
                          gate refusal are not a write, and a bare "0 written"
                          would blur all three. */}
                      <span className="text-zinc-300" title="what the sources said, who said it, and what this run did with the value">
                        {advisoryLine(
                          replyFor(advReply.values, t.path),
                          answerSources(replyFor(advReply.answers, t.path), replyFor(advReply.sources, t.path)),
                          replyFor(advReply.status, t.path)
                        )}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </MinBlock>
          <MinBlock min={minMode} here={missingHere} mine="advisory">
            {stepTracks.map((t) => (
              <div key={t.path} className={ROW_WRAP}>
                <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
                <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
                {!!t.tags.ITUNESADVISORY && !["0", "1", "2"].includes(t.tags.ITUNESADVISORY.trim()) && (
                  <span
                    className="chip bg-amber-950/40 text-amber-300 border border-amber-900 shrink-0"
                    title="ITUNESADVISORY must be 0, 1 or 2 — pick a value below to fix it"
                  >
                    invalid existing value “{t.tags.ITUNESADVISORY}”
                  </span>
                )}
                <div className="flex gap-1">
                  {["0", "1", "2"].map((v) => (
                    <button
                      key={v}
                      onClick={() => setAdvisory((a) => ({ ...a, [t.path]: v }))}
                      className={`px-2 sm:px-3 py-1 rounded text-xs border ${
                        (advisory[t.path] ?? t.tags.ITUNESADVISORY) === v
                          ? "bg-accent on-accent border-accent"
                          : "bg-panel text-zinc-400 border-border hover:border-accent/50"
                      }`}
                    >
                      {v === "0" ? "0 · clean" : v === "1" ? "1 · explicit" : "2 · safe"}
                    </button>
                  ))}
                </div>
              </div>
            ))}
            <div className="flex justify-end">
              <button className="btn-primary tap" onClick={saveAdvisory} disabled={busy}>Save advisory</button>
            </div>
          </MinBlock>
        </div>
      )}

      {/* ---------------- Step 7: finish ---------------- */}
      {step === 7 && (
        <div className="panel p-6">
          <div className="text-center">
            <Check className="h-10 w-10 text-emerald-400 mx-auto mb-3" />
            <div className="font-semibold text-lg">Import complete</div>
            <div className="text-sm text-zinc-500 mt-1">
              {uploaded.length > 1
                ? `${uploaded.length} albums were added to your library. Use the dropdown above to finish linking, matching and tagging each one.`
                : "Links, MBIDs, metadata, genres, lyrics and advisory ratings are written to the files."}
            </div>
          </div>
          <div className="mt-4">
            <ScriptChainNote preview={scriptChain} />
          </div>
          <div className="mt-5 bg-panel rounded-lg border border-border p-4">
            <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-2">
              Run scripts after import (on the new album{uploaded.length > 1 ? "s" : ""})
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              {POST_IMPORT_SCRIPTS.map((s) => (
                <label key={s.id} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={runAfterImportIds.includes(s.id)}
                    onChange={(e) =>
                      setRunAfterImport((ids) => {
                        const current = ids ?? postImportOrder;
                        if (!e.target.checked) return current.filter((i) => i !== s.id);
                        // Back to its own place in the order shown, not the end
                        // of the list — the same rule Settings and the setup
                        // wizard apply to the same order, so re-ticking a box
                        // cannot move a script behind the one it feeds.
                        const order = POST_IMPORT_SCRIPTS.map((x) => x.id);
                        const at = current.filter((i) => order.indexOf(i) < order.indexOf(s.id)).length;
                        return [...current.slice(0, at), s.id, ...current.slice(at)];
                      })
                    }
                  />
                  {s.label}
                </label>
              ))}
            </div>
            <div className="text-[10px] text-zinc-600 mt-2">
              Ticked by default in your Run All order (Settings → Script chain) — the same scripts, in the same
              order, the Optimization page and the library's Run All run. Progress shows at the top of the window.
              Scripts can also be run individually anytime from the album page.
            </div>
            <div className="flex items-center gap-2 mt-3 flex-wrap">
              <button
                className="btn-primary !py-1.5 text-xs tap"
                onClick={runAllHere}
                disabled={runningAll || scriptsRunning || (!albumPath && !uploaded.length)}
                title="Run the scripts in the order set in Settings → Optimization, on this album only"
              >
                <Wand2 className={`h-3.5 w-3.5 ${runningAll ? "animate-spin" : ""}`} />
                {runningAll ? "Running…" : "Run all scripts"}
              </button>
              <button
                className="btn-ghost !py-1.5 text-xs tap"
                onClick={runAllScripts}
                disabled={scriptsRunning || runningAll || (!albumPath && !uploaded.length)}
                title="Run the configured import chain — the same scripts a bulk or Soulseek import runs"
              >
                <Wand2 className={`h-3.5 w-3.5 ${scriptsRunning ? "animate-spin" : ""}`} />
                {scriptsRunning ? "Running…" : "Run the import chain"}
              </button>
              <span className="text-[10px] text-zinc-500">
                Run all follows your Run All order; the chain runs the import chain in its configured order. Tick boxes
                above to run just those on Done.
              </span>
            </div>
            {/* The bar over the chain, plus one row per chain id: a script
                error is text in the step, not a toast that has already gone. */}
            {(runningAll || scriptsRunning) && (
              <div className="mt-2">
                <ActionBar
                  active
                  label={act?.label ?? (runningAll ? "Running all scripts…" : "Running the import chain…")}
                  done={progress?.done}
                  total={progress?.total}
                  steps={progress?.steps}
                />
              </div>
            )}
            {runRows && runRows.length > 0 && (
              <div className="mt-2 space-y-1" role="status">
                {runRows.map((r, i) => (
                  <div key={`${r.id}-${i}`} className="flex items-start gap-2 text-[11px]">
                    <span
                      className={`chip border shrink-0 ${
                        r.ok
                          ? "bg-emerald-900/40 text-emerald-300 border-emerald-800"
                          : r.skipped
                            ? "bg-raise text-zinc-400 border-border"
                            : "bg-red-900/40 text-red-300 border-red-900"
                      }`}
                    >
                      {r.ok ? "OK" : r.skipped ? "Skipped" : "Failed"}
                    </span>
                    <span className="text-zinc-300 shrink-0">{r.label}</span>
                    <span className="text-zinc-500 min-w-0 break-words">{r.error ?? r.note ?? ""}</span>
                  </div>
                ))}
              </div>
            )}
            {finishMsg && (
              <div className="text-[11px] text-accent-soft pt-1.5" role="status">
                {finishMsg}
              </div>
            )}
          </div>
          <div className="flex justify-center gap-2 mt-5">
            {albumPath && (
              /* The album's MusicBrainz id first: it survives the rename the
                 chain's beets/organize step performs, while a raw path only
                 works until the next reorganization. */
              <Link
                to={releaseId ? `/album/mb:${encodeURIComponent(releaseId)}` : `/album/${encodeURIComponent(albumPath)}`}
                className="btn-ghost tap"
                onClick={finish}
              >
                Open album
              </Link>
            )}
            <button className="btn-primary tap" onClick={finish}>Done</button>
          </div>
        </div>
      )}

      {/* nav buttons */}
      {step > 0 && step < 7 && (
        <div className="flex flex-wrap items-center justify-between gap-2 pt-2">
          <button className="btn-ghost tap" onClick={() => setStep(step - 1)}>
            <ChevronLeft className="h-4 w-4" /> Back
          </button>
          <div className="flex flex-wrap items-center gap-2 ml-auto justify-end">
            {nextBlock && <span className="text-xs text-amber-300/90">{nextBlock}</span>}
            <button
              className="btn-primary tap"
              disabled={!canNext || busy}
              onClick={() =>
                step === 1
                  ? nextFromLinks()
                  : step === 2
                    ? confirmMatch()
                    : step === 3
                      ? saveCovers()
                      : step === 4
                        ? saveGenres()
                        : step === 5
                          ? saveLyricsStep()
                          : saveAdvisory()
              }
            >
              {step === 6 ? "Save & finish" : "Continue"} <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}

      {/* Metadata review for the album just committed. The artist is its
          parent folder — the images and descriptions the modal edits live
          there. Dismissing it never touches the wizard's own step state. */}
      {reviewPath && (
        <MetadataReviewModal
          artist={baseName(reviewPath.split("/").slice(0, -1).join("/"))}
          albumPath={reviewPath}
          onClose={() => setReviewPath(null)}
        />
      )}
    </div>
  );
}

function CloudDownloadIcon() {
  return <ExternalLink className="h-3.5 w-3.5" />;
}

/** Whole seconds since `on` went true. The clock is the half of the progress
 *  strip that always moves: an action that can report no counts still shows
 *  it is alive instead of looking stuck. */
function useElapsed(on: boolean): number {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    if (!on) {
      setSecs(0);
      return;
    }
    const t = setInterval(() => setSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [on]);
  return secs;
}

/** THE progress bar of the wizard — one component for every action.

 *  Counts come from the action's own step (a genre chain's sources, a global
 *  script run), else from the relay frame the engine publishes over the
 *  websocket the header bar already draws. With neither, the bar is
 *  indeterminate but never still: spinners, motion, and the elapsed clock. */
function ActionBar({
  label, done, total, steps, active,
}: {
  label: string;
  done?: number | null;
  total?: number | null;
  /** The whole-step pair a chained script run publishes — printed instead of
   *  a fractional count, so the readout says "3/18" (scripts). */
  steps?: number[] | null;
  active: boolean;
}) {
  const secs = useElapsed(active);
  if (!active) return null;
  const known = !!total;
  // Unrounded: a byte-weighted or sub-step fraction moves the bar between two
  // whole percents, and rounding here would freeze it exactly like the readout
  // it sits beside.
  const pct = known ? Math.min(100, ((done ?? 0) / total!) * 100) : 0;
  const stepText = fmtSteps(steps);
  return (
    <div className="flex items-center gap-2 min-w-0 w-full" role="status">
      <span className="h-3 w-3 rounded-full border-2 border-zinc-700 border-t-accent-soft animate-spin shrink-0" />
      <span className="text-[11px] text-zinc-300 min-w-0 truncate max-w-[24rem]" title={label}>
        {label}
      </span>
      <div className="h-1 flex-1 min-w-[80px] rounded-sm bg-raise overflow-hidden">
        <div
          className={`h-full bg-accent-soft ${known ? "" : "animate-pulse"}`}
          style={known ? { width: `${pct}%` } : { width: "35%" }}
        />
      </div>
      <span className="text-[10px] text-zinc-500 font-mono whitespace-nowrap tabular-nums">
        {known ? stepText ?? fmtCounts(done, total) : "…"} · {secs}s
      </span>
    </div>
  );
}

/** Exactly which optimizer scripts the import chain runs — the same chain the
 *  bulk queue and the Soulseek import use, configured in Settings → Import. */
function ScriptChainNote({ preview }: { preview?: ImportScriptsPreview }) {
  if (!preview) return <div className="text-[11px] text-zinc-600">Reading the import chain…</div>;
  const chain = preview.chain ?? [];
  return (
    <div className="text-[11px] text-zinc-500">
      {chain.length ? (
        <>
          Runs automatically after import:{" "}
          <span className="text-zinc-300">
            {chain.map((id) => preview.labels?.[String(id)] ?? `script ${id}`).join(" → ")}
          </span>{" "}
          — the chain is configurable in Settings → Import.
        </>
      ) : (
        <>The import chain is empty — albums are imported without post-processing (Settings → Import).</>
      )}
    </div>
  );
}

/** What one Submit-to-AcoustID press answered: the database's own reply, or
 *  the sentence the route refused with (409 while manual importing is off,
 *  400 without the confirm the client sends) — never a generic "failed". */
type AcoustidSubmitReply = { ok: true; result: AcoustidSubmitResult } | { ok: false; error: string };

/** The reply of one submission, in the service's own words: how many
 *  fingerprints it accepted, or the sentence it refused with — the two are
 *  never collapsed into a generic "failed". */
function SubmitReplyText({ reply }: { reply: AcoustidSubmitReply }) {
  if (!reply.ok) return <span className="text-red-300">{reply.error}</span>;
  const r = reply.result;
  if (!r.available) {
    return (
      <span className="text-amber-300">
        AcoustID refused the submission — {r.note}
        {r.code ? ` (${r.code})` : ""}. It is the USER key that submits: set it in{" "}
        <b className="text-amber-100">Settings → Import</b>.
      </span>
    );
  }
  return (
    <>
      AcoustID accepted <b className="text-zinc-300">{r.submitted}</b> of {r.tracks.total} fingerprint(s)
      {r.tracks.skipped
        ? ` · ${r.tracks.skipped} skipped (${(r.skips ?? [])
            .slice(0, 2)
            .map((s) => s.reason || s.code)
            .join("; ")})`
        : ""}
      .
    </>
  );
}

/** One line naming the tracks whose identity tags did NOT go in, from the
 *  server's own write codes (`writes`). Three names plus a count: a toast is
 *  one line, and the point is to say what failed, not to list forty files —
 *  `unsupported container: .wv (03 - track.wv)` is the shape, with the
 *  extension the server itself refuses (`mlo.acoustid.write_tags`). */
function writeProblems(problems: AcoustidWrite[]): string {
  const named = problems.slice(0, 3).map((w) => {
    const base = w.path.split(/[\\/]/).pop() ?? w.path;
    if (w.code === "unsupported_container") {
      const ext = base.includes(".") ? base.slice(base.lastIndexOf(".")).toLowerCase() : base;
      return `unsupported container: ${ext} (${base})`;
    }
    return `${(w.code ?? "write failed").replace(/_/g, " ")} (${base})`;
  });
  const rest = problems.length - named.length;
  return `${named.join(", ")}${rest > 0 ? ` and ${rest} more` : ""}`;
}

/** AcoustID stage: fingerprint the staged audio and name the release group it
 *  really is. "Use this release" hands the result back to the wizard's own
 *  release fetch + auto-match flow — there is no second tag writer. Accepting
 *  it also writes the identity pair onto the files, and THAT is what makes the
 *  second action possible: "Submit to AcoustID" publishes the pair the files
 *  carry, so it is offered per applied row only. */
function AcoustidBlock({
  match, busy, queue, canMatchAll, matchAllBusy, applied, onRun, onUse, onSubmit,
  onMatchAll,
}: {
  match: AcoustidMatch | null;
  busy: boolean;
  /** Queue mode: offer the shared "apply the chosen release to the queue". */
  queue: boolean;
  canMatchAll: boolean;
  matchAllBusy: boolean;
  /** Album paths whose accepted match was written into the files. */
  applied: Record<string, true>;
  onRun: () => void;
  onUse: (row: AcoustidAlbumMatch) => void;
  /** Publish this row's already-tagged fingerprints to AcoustID. */
  onSubmit: (row: AcoustidAlbumMatch) => Promise<AcoustidSubmitReply>;
  onMatchAll: () => void;
}) {
  // Which row is armed for the public submission (two presses, like the
  // LRCLIB publish panel: publishing to AcoustID is outward-facing, so one
  // click must never do it by accident) and what the database answered.
  const [arm, setArm] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [replies, setReplies] = useState<Record<string, AcoustidSubmitReply>>({});

  const submit = async (row: AcoustidAlbumMatch) => {
    if (arm !== row.path) {
      setArm(row.path);
      // The arm cancels itself, so a press that was not meant as a submission
      // cannot be completed by a later, unrelated click.
      window.setTimeout(() => setArm((a) => (a === row.path ? null : a)), 4000);
      return;
    }
    setArm(null);
    setSubmitting(row.path);
    const reply = await onSubmit(row);
    setReplies((r) => ({ ...r, [row.path]: reply }));
    setSubmitting(null);
  };

  return (
    <div className="panel p-4 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-semibold text-zinc-300">AcoustID fingerprint</span>
        <span className="text-xs text-zinc-500">
          — names the release group the audio actually is, before matching by hand.
        </span>
        {queue && (
          <button
            className="btn-primary !py-1 text-xs ml-auto tap"
            onClick={onMatchAll}
            disabled={matchAllBusy || !canMatchAll}
            title="Match every queued album to the release chosen below and write its metadata"
          >
            <Check className="h-3.5 w-3.5" />
            {matchAllBusy ? "Matching the queue…" : "Match the queue to the chosen release"}
          </button>
        )}
        <button
          className={`btn-ghost !py-1 text-xs tap ${queue ? "" : "ml-auto"}`}
          onClick={onRun}
          disabled={busy}
        >
          <Wand2 className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
          {busy ? "Fingerprinting…" : "Fingerprint & match"}
        </button>
      </div>
      {match && !match.available && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          Fingerprinting is unavailable — {match.note}. Add an AcoustID API key (or install fpcalc) in{" "}
          <b className="text-amber-100">Settings → Import</b>; until then search by title or paste a release
          link below.
        </div>
      )}
      {match?.available && (
        <div className="space-y-1">
          {match.albums?.map((row) => (
            <div
              key={row.path}
              className="flex items-center gap-2 bg-panel rounded border border-border px-3 py-2 text-xs flex-wrap"
            >
              <Disc3 className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
              <span className="text-zinc-400 truncate max-w-[14rem]" title={row.path}>
                {row.path.split(/[\\/]/).pop()}
              </span>
              {row.release_group_id ? (
                <>
                  <span className="text-zinc-200">{row.release_group_title}</span>
                  {row.artists?.length ? <span className="text-zinc-500">{row.artists.join(", ")}</span> : null}
                  {row.release_group_type && (
                    <span className="chip bg-raise border border-border text-zinc-400">{row.release_group_type}</span>
                  )}
                  <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">
                    {row.matched}/{row.total} tracks
                  </span>
                  {row.score != null && (
                    <span className="text-zinc-600 font-mono">score {Math.round(row.score * 100)}%</span>
                  )}
                  {/* A matched row can still be short of coverage ("3 of 5
                      track(s) could not be fingerprinted"): the release IS
                      this audio's, but not every track proved it. Muted, not a
                      warning — the count behind it is row.skips (nothing
                      fingerprintable, never retryable) and row.failures (the
                      tool or service could not answer, which a retry can). */}
                  {row.reason && (
                    <span className="text-zinc-600 truncate max-w-full" title={row.reason}>
                      {row.reason}
                    </span>
                  )}
                  {/* The tags claim another release group than the audio is.
                      Nothing was overwritten — the import keeps the release it
                      was matched to — so this is shown, not acted on. */}
                  {row.conflict && (
                    <span
                      className="chip bg-amber-900/50 text-amber-300 border border-amber-900 max-w-full truncate"
                      title={(row.conflicts ?? []).map((c) => c.reason).join(" | ")}
                    >
                      {(row.conflicts ?? []).length > 1
                        ? `${row.conflicts!.length} tag conflicts`
                        : row.conflicts?.[0]?.reason || "tags disagree with the audio"}
                    </span>
                  )}
                  <button
                    className="btn-ghost !py-0.5 text-[11px] ml-auto tap"
                    onClick={() => onUse(row)}
                    disabled={busy}
                    title="Fetch this release and auto-match the album's tracks"
                  >
                    Use this release
                  </button>
                  {/* Beside it: the ids are on the files (this row was
                      applied), so the fingerprints can be published. Two
                      presses, with the second one labelled — AcoustID's
                      database is public. */}
                  {applied[row.path] && (
                    <button
                      className={`btn-ghost !py-0.5 text-[11px] tap ${arm === row.path ? "!bg-red-600 !text-white" : ""}`}
                      onClick={() => submit(row)}
                      disabled={busy || submitting === row.path}
                      title={
                        arm === row.path
                          ? "Publishes the ACOUSTID_FINGERPRINT/ID pair already on these files to AcoustID's public database — press again to confirm"
                          : "Publish the fingerprint and recording id already on these files to AcoustID's public database (nothing is re-fingerprinted)"
                      }
                    >
                      {submitting === row.path ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <UploadCloud className="h-3.5 w-3.5" />
                      )}
                      {arm === row.path ? "Confirm — publishes publicly" : "Submit to AcoustID"}
                    </button>
                  )}
                  {arm === row.path && (
                    <button
                      className="btn-ghost !py-0.5 text-[11px] tap"
                      onClick={() => setArm(null)}
                      title="Leave the fingerprints unpublished"
                    >
                      <X className="h-3 w-3" /> Cancel
                    </button>
                  )}
                </>
              ) : row.status === "error" ? (
                // A lookup that FAILED is not a lookup that found nothing: the
                // server says which, and saying "no match" here would send the
                // user hunting for a release the app never asked about.
                <span className="text-amber-300">
                  {row.reason || row.code} — the audio was not identified (not "no match").
                </span>
              ) : row.status === "skipped" ? (
                <span className="text-zinc-500">
                  {row.reason || "nothing fingerprintable"} (skipped).
                </span>
              ) : (
                // The server's own sentence says WHY there is no match ("it
                // identified none of the 2 tracks" vs "no group owned enough
                // of the 3 identified ones"); the generic line is only for a
                // row from a server that predates it.
                <span className="text-zinc-500">
                  {row.reason || `No release group matched ${row.total} track(s)`} — search by title or paste
                  a release link below.
                </span>
              )}
              {/* Reply of the last submission for this row — the service's own
                  answer, in its own words. A refusal (no user key, or one it
                  rejects) is NOT a generic failure: Settings → Import is where
                  the key lives, so the sentence names it. */}
              {replies[row.path] && (
                <span className="basis-full text-[11px] text-zinc-500">
                  <SubmitReplyText reply={replies[row.path]} />
                </span>
              )}
            </div>
          ))}
          {match.albums?.length === 0 && (
            <div className="text-xs text-zinc-500">Nothing staged to fingerprint yet.</div>
          )}
        </div>
      )}
    </div>
  );
}

/** Disc/track number badge shown on EVERY import step: "2.07" when the disc is
 *  known, "07" when it is not. The width is fixed so the column lines up, and
 *  the title spells the pair out for hover text. */
function TrackNoBadge({ disc, track }: { disc: number | null; track: number | null }) {
  const tt = track != null ? String(track).padStart(2, "0") : "—";
  const label =
    disc != null && track != null
      ? `Disc ${disc}, track ${track}`
      : disc != null
        ? `Disc ${disc}, track unknown`
        : track != null
          ? `Track ${track} (disc unknown)`
          : "No track number";
  return (
    <span className="text-xs text-zinc-600 w-12 shrink-0 tabular-nums" title={label}>
      {disc != null ? `${disc}.` : ""}
      {tt}
    </span>
  );
}

/** Collapsible per-disc section header + body, shared by Match and Genres. */
function DiscSection({
  disc,
  count,
  collapsed,
  onToggle,
  extra,
  children,
}: {
  disc: number | null;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  extra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div
        className="flex flex-wrap items-center gap-1.5 px-1 pt-2 text-xs font-bold uppercase tracking-wider text-zinc-400 cursor-pointer select-none"
        onClick={onToggle}
      >
        <button className="p-0.5 text-zinc-500 hover:text-white">
          {collapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </button>
        <Disc3 className="h-3.5 w-3.5 text-zinc-500" />
        {disc ? `Disc ${disc}` : "Unmatched"}
        <span className="font-normal text-zinc-600 normal-case">
          {count} track{count === 1 ? "" : "s"}
        </span>
        {extra && (
          <span className="ml-auto flex flex-wrap items-center gap-1.5 justify-end normal-case font-normal" onClick={(e) => e.stopPropagation()}>
            {extra}
          </span>
        )}
      </div>
      {!collapsed && children}
    </div>
  );
}

/** One row in the import file list: image thumbnails for artwork, and a
 * "track cover" hint when an image shares its stem with an audio file in
 * the same album group (that is how per-track covers are added). */
function ImportFileRow({ f, gi, albums, groupFiles, selectable, excluded, onToggleExcluded, onMove }: {
  f: ImportFile;
  gi: number;
  albums: AlbumGroup[];
  groupFiles: ImportFile[];
  selectable: boolean;
  excluded: boolean;
  onToggleExcluded: () => void;
  onMove: (to: number) => void;
}) {
  const isImage = /\.(jpg|jpeg|png|webp|bmp|gif|tiff|tif|avif|heic|heif|jxl|svg)$/i.test(f.relPath);
  const [thumb, setThumb] = useState<string | null>(null);
  useEffect(() => {
    if (!isImage || !f.file) return;
    const url = URL.createObjectURL(f.file);
    setThumb(url);
    return () => URL.revokeObjectURL(url);
  }, [isImage, f.file]);
  const coverFor = (() => {
    if (!isImage) return null;
    const stem = f.relPath.split("/").pop()!.replace(/\.[^.]+$/, "").toLowerCase();
    if (["cover", "front", "folder"].includes(stem)) return "album cover";
    const hit = groupFiles.find((x) => x !== f && AUDIO_RE.test(x.relPath) && x.relPath.split("/").pop()!.replace(/\.[^.]+$/, "").toLowerCase() === stem);
    return hit ? `track cover for ${hit.relPath.split("/").pop()}` : null;
  })();
  return (
    <div className={`${ROW} text-xs ${excluded ? "opacity-45" : ""}`}>
      {selectable && (
        <input
          type="checkbox"
          checked={!excluded}
          onChange={onToggleExcluded}
          title={excluded ? "Excluded — click to include this file again" : "Untick to leave this file out (partial album import)"}
          aria-label={`Include ${f.relPath}`}
        />
      )}
      {isImage && thumb ? (
        <img src={thumb} alt="" className="h-8 w-8 rounded bg-raise border border-border object-cover shrink-0" />
      ) : isImage ? (
        <span className="h-8 w-8 rounded bg-raise border border-border shrink-0 flex items-center justify-center text-zinc-600 text-[9px]">IMG</span>
      ) : null}
      <span className="flex-1 min-w-0" title={f.relPath}>
        <span className={`block break-words ${excluded ? "text-zinc-500 line-through" : "text-zinc-400"}`}>{f.relPath}</span>
        {excluded && <span className="block text-[10px] text-amber-300/80">left out — will show greyed out on the album page</span>}
        {!excluded && coverFor && <span className="block text-[10px] text-accent-soft">→ {coverFor}</span>}
      </span>
      <select
        className="input !w-auto !py-1 text-xs shrink-0 tap max-w-[45%]"
        value={gi}
        onChange={(e) => onMove(Number(e.target.value))}
      >
        {albums.map((a, i) => (
          <option key={i} value={i}>{a.name.trim() || `Album ${i + 1}`}</option>
        ))}
      </select>
    </div>
  );
}
