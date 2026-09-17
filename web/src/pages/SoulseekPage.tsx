import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownUp, Download, Eye, EyeOff, FolderOpen, Loader2, Play, Power, RefreshCw, Search,
  User, Zap, Square, FileCheck2, FileVideo, Music2, Save, Tag, Trash2, PackageOpen,
  Star, Plus, CheckCircle2, CircleDashed, AlertTriangle, ExternalLink, RotateCw, ChevronDown, ChevronRight, Link2,
  MessageSquare,
} from "lucide-react";
import { api } from "../api";
import type { SlskAutoFile, SlskAutoProgress, SlskConversation, SlskDownloads, SlskMessage, SlskSearchProgress, SlskTransfer } from "../api";
import { toast } from "../store";
import { EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import Modal from "../components/Modal";
import type { Wish } from "../types";

interface SlskFile {
  username: string;
  file: string;
  size: number;
  bitrate: number | null;
  duration: number | null;
  vbr: boolean | null;
  slot: boolean;
  speed: number;
  queue: number;
  /** codec hint from the peer (slskd reports the extension + sample facts) */
  ext?: string;
  bit_depth?: number | null;
  sample_rate?: number | null;
}

const fmtRate = (n: number | null | undefined) => {
  if (!n) return "0 kB/s";
  if (n > 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB/s`;
  return `${(n / 1024).toFixed(0)} kB/s`;
};
const fmtSize = (n: number) => {
  if (!n) return "—";
  if (n > 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  if (n > 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024).toFixed(0)} kB`;
};
const fmtDur = (s: number | null) => {
  if (!s) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};
const fileName = (p: string) => p.replace(/^.*[\\/]/, "");
const dirName = (p: string) => p.replace(/[^\\/]*$/, "");
const extOf = (p: string) => (p.match(/\.([a-z0-9]+)$/i)?.[1] ?? "").toUpperCase();

/** One shared remote folder — the unit worth downloading for album rips. */
interface SlskGroup {
  key: string;
  username: string;
  dir: string;
  files: SlskFile[];
  audio: SlskFile[]; // files that are music (logs/cues/jpegs excluded)
  format: string; // dominant audio extension, e.g. "FLAC"
  lossless: boolean;
  hasLog: boolean;
  hasCue: boolean;
  totalSize: number;
  slotFree: boolean;
  minQueue: number;
  isCdRip: boolean; // log + cue present → a verifiable CD rip
}

/** Every audio container the network offers. Lossy and lossless both count —
 *  the search shows what is there and marks which is which. */
const AUDIO_EXTS: Record<string, true> = {
  FLAC: true, WAV: true, AIFF: true, AIF: true, ALAC: true, APE: true, WV: true,
  SHN: true, TTA: true, DSF: true, DFF: true,
  M4A: true, MP4: true, AAC: true, MP3: true, OGG: true, OGA: true, OPUS: true,
  WMA: true, MPC: true, MP2: true, MKA: true,
};
/** Lossless by extension. M4A/MP4 are the ambiguous pair: the container holds
 *  ALAC (lossless) or AAC (lossy), so they only count as lossless when the
 *  peer reports a bit depth — AAC does not carry one. */
const LOSSLESS_EXTS: Record<string, true> = {
  FLAC: true, WAV: true, AIFF: true, AIF: true, ALAC: true, APE: true, WV: true,
  SHN: true, TTA: true, DSF: true, DFF: true,
};
const isLossless = (f: SlskFile) => {
  const e = (f.ext || extOf(f.file)).toUpperCase();
  if (e === "M4A" || e === "MP4") return Number(f.bit_depth ?? 0) > 0 && !f.bitrate;
  return LOSSLESS_EXTS[e] === true;
};

function groupResults(results: SlskFile[]): SlskGroup[] {
  const map = new Map<string, SlskGroup>();
  for (const f of results) {
    const dir = dirName(f.file);
    const key = `${f.username}\u0000${dir}`;
    let g = map.get(key);
    if (!g) {
      g = {
        key, username: f.username, dir, files: [], audio: [],
        format: "", lossless: false, hasLog: false, hasCue: false,
        totalSize: 0, slotFree: false, minQueue: Number.MAX_SAFE_INTEGER, isCdRip: false,
      };
      map.set(key, g);
    }
    g.files.push(f);
    const ext = (f.ext || extOf(f.file)).toUpperCase();
    if (ext === "LOG") g.hasLog = true;
    if (ext === "CUE") g.hasCue = true;
    if (AUDIO_EXTS[ext]) g.audio.push(f);
    g.totalSize += f.size || 0;
    g.slotFree ||= f.slot;
    g.minQueue = Math.min(g.minQueue, f.queue || 0);
  }
  const groups = [...map.values()];
  for (const g of groups) {
    // dominant audio format by file count; lossless if that format is (a
    // folder is lossy only when nothing in it decodes losslessly)
    const counts = new Map<string, number>();
    for (const f of g.audio) {
      const e = (f.ext || extOf(f.file)).toUpperCase();
      counts.set(e, (counts.get(e) ?? 0) + 1);
    }
    g.format = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "";
    g.lossless = g.audio.some(isLossless);
    g.isCdRip = g.hasLog && g.hasCue;
    g.minQueue = g.minQueue === Number.MAX_SAFE_INTEGER ? 0 : g.minQueue;
  }
  // Best imports first: verifiable CD rips, then lossless, then free slots /
  // shortest queues — this ordering is the "well thought out" default.
  groups.sort(
    (a, b) =>
      Number(b.isCdRip) - Number(a.isCdRip) ||
      Number(b.lossless) - Number(a.lossless) ||
      Number(a.slotFree ? 0 : 1) - Number(b.slotFree ? 0 : 1) ||
      a.minQueue - b.minQueue
  );
  return groups;
}

type FilterId = "all" | "cdrip" | "lossless" | "lossy";
const FILTERS: { id: FilterId; label: string }[] = [
  { id: "all", label: "All" },
  { id: "cdrip", label: "CD rips (log + cue)" },
  { id: "lossless", label: "Lossless" },
  { id: "lossy", label: "Lossy" },
];

function groupMatches(g: SlskGroup, f: FilterId, codec?: string): boolean {
  if (codec && g.audio.every((x) => (x.ext || extOf(x.file)).toUpperCase() !== codec)) return false;
  if (f === "cdrip") return g.isCdRip;
  if (f === "lossless") return g.lossless;
  if (f === "lossy") return !g.lossless;
  return true;
}

function GroupBadges({ g }: { g: SlskGroup }) {
  return (
    <span className="inline-flex items-center gap-1 shrink-0">
      {g.format && (
        <span className={`chip text-[9px] border ${g.lossless ? "bg-sky-900/40 text-sky-300 border-sky-800" : "bg-zinc-800 text-zinc-300 border-zinc-700"}`}>
          {g.format}
        </span>
      )}
      {g.hasLog && (
        <span className="chip text-[9px] bg-emerald-900/50 text-emerald-300 border border-emerald-800">log</span>
      )}
      {g.hasCue && (
        <span className="chip text-[9px] bg-emerald-900/50 text-emerald-300 border border-emerald-800">cue</span>
      )}
      {g.isCdRip && (
        <span className="chip text-[9px] bg-accent on-accent border border-transparent font-semibold">CD rip</span>
      )}
    </span>
  );
}

/** Strip a MusicBrainz URL down to the bare release MBID. */
const releaseMbid = (s: string) =>
  /(?:release\/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(s.trim())?.[1] ?? "";

/** Last few manual searches, newest first — click to re-run. */
const RECENT_KEY = "mlso.recentSearches";
const RECENT_MAX = 10;

function loadRecentSearches(): string[] {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string").slice(0, RECENT_MAX) : [];
  } catch {
    return []; // unset or tampered value — an empty history is not an error
  }
}

/** Dedupe by exact query text, newest first, capped at RECENT_MAX. */
function saveRecentSearch(list: string[], q: string): string[] {
  const next = [q, ...list.filter((x) => x !== q)].slice(0, RECENT_MAX);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* storage full or disabled (private mode) — history is best-effort */
  }
  return next;
}

/** Saved credentials exist and slskd isn't logged in (yet) — usually the
 * few seconds a reconnect takes, sometimes a Soulseek-server cooldown after
 * a disconnect. Shows what the app is doing and a one-click retry that does
 * NOT restart slskd (restarting resets the server's cooldown and made the
 * old stop → start → login loop stop working). */
function ReconnectingCard({ username, password, onDone }: {
  username: string;
  password: string;
  onDone: () => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState(false);
  const retry = async () => {
    setBusy(true);
    try {
      const r = await api.soulseekLogin(username, password);
      toast(r.message);
      if (r.logged_in) onDone();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };
  if (showForm) return <LoginCard onDone={onDone} initialUsername={username} initialPassword={password} />;
  return (
    <div className="panel border-amber-900/50 flex items-center gap-2 flex-wrap text-xs">
      <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-300 shrink-0" />
      <span className="text-zinc-300">
        Reconnecting to Soulseek as <b>{username}</b>…
      </span>
      <span className="text-zinc-600">
        the network sometimes enforces a short cooldown after a disconnect
      </span>
      <div className="ml-auto flex gap-1.5">
        <button className="btn-ghost !py-1 text-xs" onClick={retry} disabled={busy}>
          {busy ? "Reconnecting…" : "Reconnect now"}
        </button>
        <button className="btn-ghost !py-1 text-xs" onClick={() => setShowForm(true)}>
          Different account
        </button>
      </div>
    </div>
  );
}

/** Sign in to the Soulseek network (or register a brand-new username — the
 * server creates accounts on first login). Shown whenever slskd is running
 * but not logged in. Credentials are SAVED by the server, so the form comes
 * prefilled and future starts reconnect on their own. */
function PortConflictCard({ message, otherUser }: {
  message: string;
  otherUser?: string | null;
}) {
  return (
    <div className="panel border-red-900/50">
      <div className="text-xs font-semibold uppercase tracking-wider text-red-300 mb-1.5 flex items-center gap-1.5">
        <AlertTriangle className="h-3.5 w-3.5" /> Soulseek port already in use
      </div>
      <p className="text-[11px] text-zinc-400 mb-2.5">
        {message}.
        {otherUser ? (
          <>
            {" "}That instance is configured for the Soulseek account <span className="text-zinc-200">{otherUser}</span>,
            which is a different account from this app's — it cannot be reused.
          </>
        ) : null}
      </p>
      <p className="text-[11px] text-zinc-500 mb-3">
        slskd permits a single running instance per machine, so this app cannot
        start its own while that one is up. Quit the other application (or stop
        its slskd) and press Start again. This app never stops the other
        program's slskd for you.
      </p>
      <a className="btn-ghost text-xs" href="/settings">
        <ExternalLink className="h-3.5 w-3.5" /> Open Settings
      </a>
    </div>
  );
}

function LoginCard({ onDone, initialUsername, initialPassword, initialError }: {
  onDone: () => void;
  initialUsername?: string;
  initialPassword?: string;
  initialError?: string | null;
}) {
  const [username, setUsername] = useState(initialUsername ?? "");
  const [password, setPassword] = useState(initialPassword ?? "");
  const [showPw, setShowPw] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(initialError ?? null);
  const [failed, setFailed] = useState(!!initialError);

  const login = async () => {
    if (!username.trim() || !password) {
      toast("Enter a username and password");
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const r = await api.soulseekLogin(username.trim(), password);
      setResult(r.message);
      setFailed(!r.logged_in);
      if (r.logged_in) {
        toast(r.message);
        onDone();
      }
    } catch (e) {
      setResult(String(e));
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel border-amber-900/50">
      <div className="text-xs font-semibold uppercase tracking-wider text-amber-300 mb-1.5">
        Not logged in to Soulseek
      </div>
      <p className="text-[11px] text-zinc-500 mb-2.5">
        Enter your Soulseek credentials to search, download and share. If the
        username doesn't exist yet, the network registers it automatically on
        first login — same button, no separate sign-up.
      </p>
      <div className="flex gap-2 flex-wrap">
        <input
          className="input w-52"
          placeholder="Soulseek username"
          value={username}
          autoComplete="username"
          onChange={(e) => setUsername(e.target.value)}
        />
        <div className="relative">
          <input
            className="input w-52 pr-9"
            placeholder="Password"
            type={showPw ? "text" : "password"}
            value={password}
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !busy && login()}
          />
          <button
            className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-200"
            onClick={() => setShowPw(!showPw)}
            title={showPw ? "Hide password" : "Show password"}
            type="button"
          >
            {showPw ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
          </button>
        </div>
        <button className="btn-primary" onClick={login} disabled={busy}>
          {busy ? "Connecting…" : "Log in / create account"}
        </button>
      </div>
      {result && (
        <div className={`text-[11px] mt-2 ${failed ? "text-red-300" : "text-zinc-400"}`}>{result}</div>
      )}
    </div>
  );
}

/** Live progress for the auto-import search stage: how far into the current
 *  query's response window it is, and how much the network has answered. */
function SearchProgress({ s }: { s: SlskSearchProgress }) {
  const wait = Math.max(0, s.wait || 0);
  // The readout never runs past the window: the server clamps `elapsed` and
  // sends `remaining`, and when an older server sends neither the elapsed
  // being printed is clamped here — that is what produced "16s / 15s".
  const elapsed = Math.min(Math.max(0, s.elapsed || 0), wait);
  const left = typeof s.remaining === "number" ? Math.max(0, s.remaining) : Math.max(0, wait - elapsed);
  const pct = wait > 0 ? Math.min(100, Math.round((elapsed / wait) * 100)) : 0;
  return (
    <div className="mt-2">
      <div className="flex items-center gap-2 text-[11px] text-zinc-500">
        <span className="truncate" title={s.query}>
          query “{s.query}” ·{" "}
          {typeof s.remaining === "number" ? `${left}s left` : `${elapsed}s / ${wait}s`}
        </span>
        <span className="ml-auto shrink-0 text-zinc-400">
          {s.responses} responses · {s.files} files
        </span>
      </div>
      <div className="mt-1 h-1.5 rounded-sm bg-border/70 overflow-hidden">
        <div
          className="h-full bg-accent transition-[width] duration-500 ease-linear"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

/** One file of the running download — the same row markup the Downloads tab
 *  uses, so both views of a transfer read alike. */
function ProgressFileRow({ f }: { f: SlskAutoFile }) {
  const p = Math.max(0, Math.min(100, Math.round(f.percent ?? 0)));
  const ok = f.done === true || /succeed|complet/i.test(f.state ?? "");
  return (
    <div className="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-white/[0.04] text-xs">
      <div className="flex-1 min-w-0 truncate text-zinc-200" title={f.name}>{fileName(f.name ?? "")}</div>
      <div className="w-28 shrink-0 h-1.5 rounded-sm bg-border/70 overflow-hidden">
        <div className={`h-full ${ok ? "bg-emerald-500" : "bg-accent"}`} style={{ width: `${p}%` }} />
      </div>
      <span className="text-zinc-500 w-24 text-right shrink-0">
        {fmtSize(f.bytes ?? 0)} / {fmtSize(f.size ?? 0)}
      </span>
      <span className="w-16 text-right shrink-0">
        {ok ? (
          <span className="chip text-[9px] bg-emerald-900/40 text-emerald-300 border border-emerald-800">done</span>
        ) : /inprogress/i.test(f.state ?? "") ? (
          <span className="chip text-[9px] bg-sky-900/40 text-sky-300 border border-sky-800">{p}%</span>
        ) : (
          <span className="chip text-[9px] bg-raise border border-border text-zinc-400">
            {(f.state ?? "").toLowerCase() || "queued"}
          </span>
        )}
      </span>
    </div>
  );
}

/** What the download stage is actually doing: files/bytes done, live speed,
 *  ETA, the peer it is pulling from, and a collapsible per-file list. */
function AutoProgress({ p }: { p: SlskAutoProgress }) {
  const done = p.files_done ?? 0;
  const total = p.files_total ?? 0;
  // Server percentage when present, derived from bytes otherwise — clamped either way.
  const raw = p.percent ?? (p.size ? (100 * (p.bytes ?? 0)) / p.size : 0);
  const pct = Math.max(0, Math.min(100, Math.round(raw)));
  const files = p.files ?? [];
  const shown = files.slice(0, 8);
  return (
    <div className="mt-2 rounded-lg border border-border bg-panel/60 p-2.5">
      <div className="flex items-center gap-2 text-[11px]">
        <span className="font-medium text-zinc-300">{p.phase || "download"}</span>
        {p.username && <span className="text-zinc-500 truncate" title={p.dir}>· {p.username}</span>}
        <span className="ml-auto shrink-0 text-zinc-400">{done} / {total} files · {pct}%</span>
      </div>
      <div className="mt-1 h-1.5 rounded-sm bg-border/70 overflow-hidden">
        <div className="h-full bg-accent transition-[width] duration-500 ease-linear" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 flex items-center gap-3 text-[10px] text-zinc-500">
        <span>{fmtSize(p.bytes ?? 0)} / {fmtSize(p.size ?? 0)}</span>
        <span>{fmtRate(p.speed)}</span>
        <span>ETA {fmtDur(p.eta_s ?? null)}</span>
      </div>
      {files.length > 0 && (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-[11px] text-zinc-500">{files.length} file(s)</summary>
          <div className="mt-1 max-h-52 overflow-auto">
            {shown.map((f, i) => <ProgressFileRow key={`${f.name ?? ""}\u0000${i}`} f={f} />)}
            {files.length > shown.length && (
              <div className="px-2 py-1 text-[11px] text-zinc-600">+{files.length - shown.length} more</div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

/** Live view of the auto-import job (search → log test → download → audit → import). */
function AutoPanel({ initialMbid }: { initialMbid?: string }) {
  const { data: job, refetch } = useQuery({
    queryKey: ["soulseekAuto"],
    queryFn: api.soulseekAutoStatus,
    // "confirm" is an active state too — the job is parked waiting for the
    // lossy-only go-ahead, so the card must appear promptly.
    refetchInterval: (q) => (q.state.data?.state === "running" || q.state.data?.state === "confirm" ? 2000 : 15000),
  });
  const running = job?.state === "running" || job?.state === "confirm";
  const navigate = useNavigate();
  const [mbid, setMbid] = useState(initialMbid ?? "");
  const [queries, setQueries] = useState("");
  const [answering, setAnswering] = useState(false);

  const start = async () => {
    const id = releaseMbid(mbid);
    if (!id) {
      toast("Paste a MusicBrainz release URL or MBID");
      return;
    }
    try {
      await api.soulseekAutoStart({
        release_mbid: id,
        queries: queries.split(";").map((s) => s.trim()).filter(Boolean) || undefined,
      });
      toast("Auto-import started");
      refetch();
    } catch (e) {
      toast.error(String(e));
    }
  };

  const cancel = async () => {
    try {
      await api.soulseekAutoCancel();
      toast("Cancelling after the current step…");
      refetch();
    } catch (e) {
      toast.error(String(e));
    }
  };

  /** Answer the confirm prompt. Both variants share this endpoint: for a lossy
   *  downgrade accept downloads the lossy copy, for an empty search accept
   *  parks the release in the wish list (the background worker keeps looking,
   *  so declining is the only way to actually stop). */
  const answer = async (accept: boolean) => {
    setAnswering(true);
    const noResults = job?.confirm?.reason === "no_results";
    try {
      await api.soulseekAutoConfirm(accept);
      toast(noResults
        ? (accept ? "Moving it to wishes — the search keeps running" : "Stopping — nothing was downloaded")
        : (accept ? "Downloading the lossy copy" : "Stopped — waiting for a lossless copy"));
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAnswering(false);
    }
  };

  // Announce the outcome once, on the poll that first sees a terminal state —
  // a job that fails after ten minutes is otherwise invisible unless the panel
  // happens to be open. The previous state lives in a ref so a re-render or a
  // late poll cannot repeat the toast, and a freshly started job re-arms it by
  // passing through "running" again; a page loaded while a job has already
  // ended therefore stays quiet instead of replaying an old finish.
  const prevState = useRef<string | null>(null);
  useEffect(() => {
    const st = job?.state;
    if (!st) return;
    const prev = prevState.current;
    prevState.current = st;
    if (prev !== "running" && prev !== "confirm") return;
    if (st === "running" || st === "confirm") return;
    if (st === "done") {
      if (job?.result?.wished) {
        toast.success("Added to wishes — the background search keeps looking for it");
        return;
      }
      const album = fileName(job?.result?.album_path ?? job?.result?.staging_path ?? "");
      toast(album ? `Imported ${album}` : "Auto-import finished");
      return;
    }
    if (st === "cancelled") {
      toast("Auto-import stopped");
      return;
    }
    // A declined prompt ends the job as an error too, so a stop the user asked
    // for is not announced as a failure.
    const err = String(job?.result?.error || job?.stage || "see the log");
    toast(/declined|nothing was downloaded/i.test(err) ? `Stopped — ${err}` : `Auto-import failed — ${err}`);
  }, [job]);

  // Only the no_results prompt carries `waited`; the lossy card has no timing.
  const waited = job?.confirm?.waited ?? 0;
  const waitedTxt = waited >= 90 ? `${Math.round(waited / 60)} min` : waited >= 1 ? `${Math.round(waited)}s` : "";

  const r = job?.release;
  // The wizard needs the folder it should tag; staging_path is the fallback an
  // unorganized job leaves in the result.
  const tagPath = job?.result?.album_path ?? job?.result?.staging_path ?? "";
  return (
    <div className="panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
          <Zap className="h-3.5 w-3.5" /> Auto-import a MusicBrainz release
        </div>
        {running && (
          <button className="btn-ghost !py-1 text-xs text-red-300" onClick={cancel}>
            <Square className="h-3 w-3" /> Stop
          </button>
        )}
      </div>
      <div className="text-[11px] text-zinc-500 mb-2.5">
        Finds the release on the network by its identifiable traits (catalog number for CDs,
        title + year for digital media — customizable in Settings), tests the rip logs before
        committing, downloads, audits against the logs, and imports it fully tagged.
        Lossless folders are preferred — if only lossy copies exist you are asked before
        anything is downloaded.
      </div>
      {!running && (
        <div className="flex gap-2 flex-wrap">
          <input
            className="input flex-1 min-w-[240px]"
            placeholder="MusicBrainz release URL or MBID (e.g. https://musicbrainz.org/release/…)"
            value={mbid}
            onChange={(e) => setMbid(e.target.value)}
          />
          <input
            className="input w-56"
            placeholder="Custom queries (; separated, optional)"
            value={queries}
            onChange={(e) => setQueries(e.target.value)}
            title="Override the search terms for this run. Fields: artist album year country catalognumber barcode label"
          />
          <button className="btn-primary" onClick={start}>
            <Zap className="h-4 w-4" /> Auto-import
          </button>
        </div>
      )}
      {job?.state !== "idle" && (
        <div className="mt-3 rounded-lg border border-border bg-panel/50 p-3">
          {r?.title && (
            <div className="text-xs text-zinc-300 mb-1.5">
              <span className="text-zinc-500">Target:</span> {r.artist} — {r.title}
              {r.catalog_number ? ` · ${r.catalog_number}` : ""}
              {r.date ? ` (${r.date})` : ""} · {r.media}
            </div>
          )}
          <div className="text-xs font-medium text-zinc-200">{job?.stage || job?.state}</div>
          {job?.search && <SearchProgress s={job.search} />}
          {/* Only set while a download is in flight, and absent on older
              servers — the panel renders fine without it. */}
          {job?.progress && <AutoProgress p={job.progress} />}
          {job?.state === "error" && (
            <div className="mt-2 rounded-lg border border-red-800 bg-red-950/40 p-2.5">
              <div className="text-xs font-semibold text-red-300">
                Failed{job.stage ? ` — ${job.stage}` : ""}
              </div>
              <div className="text-[11px] text-red-200/80 mt-1 break-words">
                {job.result?.error || "The job stopped before it finished — see the log below."}
              </div>
              <button
                className="btn-ghost !py-1 text-xs mt-2 text-red-300"
                disabled={!releaseMbid(mbid)}
                onClick={start}
                title="Run the same release again with the values in the form above"
              >
                <RotateCw className="h-3.5 w-3.5" /> Retry
              </button>
            </div>
          )}
          {job?.state === "cancelled" && (
            <div className="mt-2 rounded-lg border border-border bg-panel/60 p-2.5">
              <div className="text-xs font-semibold text-zinc-300">Cancelled</div>
              <div className="text-[11px] text-zinc-500 mt-1">
                Stopped during {job.stage || "the current step"} — nothing else was downloaded.
              </div>
              <button
                className="btn-ghost !py-1 text-xs mt-2"
                disabled={!releaseMbid(mbid)}
                onClick={start}
                title="Run the same release again with the values in the form above"
              >
                <RotateCw className="h-3.5 w-3.5" /> Start again
              </button>
            </div>
          )}
          {job?.state === "confirm" && job?.confirm && (job.confirm.reason === "no_results" ? (
            // A search that came back empty: the release is not on the network
            // right now, so the useful answer is "keep looking". Accept parks it
            // in the wish list, where the same search runs on the worker's own
            // schedule with no further input. Decline is the only way to stop.
            <div className="mt-2 rounded-lg border border-amber-700/60 bg-amber-950/30 p-2.5">
              <div className="text-xs font-semibold text-amber-300 mb-1">
                Nothing usable found{waitedTxt ? ` in ${waitedTxt}` : ""}
              </div>
              <div className="text-[11px] text-zinc-400 mb-2">
                Every candidate this search turned up was rejected or incomplete. Moving the
                release to wishes keeps the same search running in the background — nothing else
                to answer, and it is imported automatically once a verified copy shows up.
                Stopping instead abandons this release until you ask for it again.
              </div>
              {(job.confirm.queries ?? []).length > 0 && (
                <div className="space-y-1 mb-2">
                  {(job.confirm.queries ?? []).map((q, i) => (
                    <div key={i} className="text-[11px] text-zinc-500 truncate" title={q}>searched “{q}”</div>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-2">
                <button className="btn-primary !py-1 text-xs" onClick={() => answer(true)} disabled={answering}>
                  <Star className="h-3.5 w-3.5" /> Move to wishes
                </button>
                <button className="btn-ghost !py-1 text-xs" onClick={() => answer(false)} disabled={answering}>
                  No, stop
                </button>
              </div>
            </div>
          ) : (
            <div className="mt-2 rounded-lg border border-amber-700/60 bg-amber-950/30 p-2.5">
              <div className="text-xs font-semibold text-amber-300 mb-1">
                Only lossy copies found ({(job.confirm.formats ?? []).filter(Boolean).join(", ") || "lossy"})
              </div>
              <div className="text-[11px] text-zinc-400 mb-2">
                No lossless folder passed the search for this release. Download the best
                lossy copy anyway, or stop and wait for a lossless one?
              </div>
              <div className="space-y-1 mb-2">
                {(job.confirm.candidates ?? []).map((c) => (
                  <div key={`${c.username}\u0000${c.dir}`} className="text-[11px] text-zinc-500 truncate" title={c.dir}>
                    {c.format || "?"} · {c.matched}/{c.expected} tracks · {fmtSize(c.size)} · {c.username} · …{c.dir.slice(-40)}
                  </div>
                ))}
              </div>
              <div className="flex items-center gap-2">
                <button className="btn-primary !py-1 text-xs" onClick={() => answer(true)} disabled={answering}>
                  <Download className="h-3.5 w-3.5" /> Download lossy anyway
                </button>
                <button className="btn-ghost !py-1 text-xs" onClick={() => answer(false)} disabled={answering}>
                  No, wait for lossless
                </button>
              </div>
            </div>
          ))}
          <div className="mt-1.5 max-h-44 overflow-auto font-mono text-[10px] leading-relaxed text-zinc-500 space-y-0.5">
            {(job?.log ?? []).map((l: any, i: number) => (
              <div key={i} className={l.msg.startsWith("ERROR") ? "text-red-400" : l.msg.startsWith("  ✕") ? "text-red-300" : undefined}>
                <span className="text-zinc-700 mr-1.5">{l.t}</span>{l.msg}
              </div>
            ))}
          </div>
          {(job?.attempts ?? []).length > 0 && (
            <details className="mt-1.5 text-[11px] text-zinc-500">
              <summary className="cursor-pointer">{job?.attempts?.length} rejected candidate(s)</summary>
              <div className="mt-1 space-y-0.5">
                {(job?.attempts ?? []).map((a, i) => (
                  <div key={i} title={a.dir}>…{String(a.dir).slice(-40)} — {a.reason}</div>
                ))}
              </div>
            </details>
          )}
          {job?.state === "done" && (job.result?.wished ? (
            // A wish handoff ends the job done but with no album on disk — the
            // import row would otherwise show an empty name and a dead button.
            <div className="mt-2 text-[11px] text-amber-300">
              Moved to wishes — the background search keeps looking for it.
            </div>
          ) : (
            <div className="mt-2 flex items-center gap-2 flex-wrap">
              <button
                className="btn-primary !py-1 text-xs"
                disabled={!tagPath}
                onClick={() => {
                  stopPreviews();
                  navigate(`/import?album=${encodeURIComponent(tagPath)}`);
                }}
                title={tagPath
                  ? `Open the import wizard for ${tagPath} — covers, lyrics and advisory`
                  : "The job result carried no album folder — see the log below"}
              >
                <Tag className="h-3.5 w-3.5" /> Tag album
              </button>
              <span className="text-[11px] text-emerald-400">
                Imported {(job.result?.album_path ?? "").split(/[\\/]/).pop()}
                {!job.result?.organized ? " (organize failed — run it from the album page)" : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** One folder in a browsed share. */
type SlskBrowseDir = { directory: string; files: { filename: string; size: number }[] };

/** A peer's shared tree (slskd browse) — pick a folder to queue as-is, or hand
 *  it to auto-import. Big shares take a moment to enumerate, so the answer is
 *  cached per user and the folder list is filterable and paged. */
function BrowseModal({ username, onAuto, onClose }: {
  username: string;
  onAuto: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["soulseekBrowse", username],
    queryFn: () => api.soulseekBrowse(username),
    staleTime: 60000,
  });
  const [text, setText] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [limit, setLimit] = useState(200);
  // Folders ticked for "Queue selected" — a whole-share rip is dozens of
  // folders, so one request per folder is not an option.
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const dirs = data?.directories ?? [];
  const needle = text.trim().toLowerCase();
  const shown = needle ? dirs.filter((d) => d.directory.toLowerCase().includes(needle)) : dirs;
  const totalBytes = dirs.reduce((n, d) => n + d.files.reduce((m, f) => m + (f.size || 0), 0), 0);

  const toggle = (dir: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });

  const togglePick = (dir: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });

  const queue = async (d: SlskBrowseDir) => {
    setBusy(d.directory);
    try {
      const r = await api.soulseekDownload(username, d.files.map((f) => ({ filename: f.filename, size: f.size })));
      toast.success(`Queued ${r.queued} file(s) from ${username}`);
      qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  /** One file row's Download: slskd takes a single-entry file list. */
  const queueFile = async (f: { filename: string; size: number }) => {
    setBusy(f.filename);
    try {
      const r = await api.soulseekDownload(username, [{ filename: f.filename, size: f.size }]);
      toast.success(`Queued ${r.queued} file(s) from ${username}`);
      qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  /** Every ticked folder in one request — the server walks each folder. */
  const queuePicked = async () => {
    const files = dirs
      .filter((d) => picked.has(d.directory))
      .flatMap((d) => d.files.map((f) => ({ filename: f.filename, size: f.size })));
    if (files.length === 0) return;
    setBusy("*");
    try {
      const r = await api.soulseekDownloadBulk(username, files);
      toast.success(`Queued ${r.queued} of ${files.length} file(s) from ${picked.size} folder(s)`);
      setPicked(new Set());
      qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  /** The whole share — slskd enumerates their file list server-side, so this
   *  can scan for minutes on a big user. */
  const queueUser = async () => {
    setBusy("*");
    try {
      const r = await api.soulseekDownloadUser(username);
      toast.success(
        `Queued ${r.queued} of ${r.scanned} file(s) from ${username}` +
        (r.skipped ? ` · ${r.skipped} skipped` : "")
      );
      qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const auto = async (d: SlskBrowseDir) => {
    setBusy(d.directory);
    try {
      await api.soulseekAutoStart({ username, target_dir: d.directory });
      toast(`Auto-importing from ${username} · ${d.directory.split(/[\\/]/).filter(Boolean).pop() ?? ""}`);
      qc.invalidateQueries({ queryKey: ["soulseekAuto"] });
      onAuto();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Modal
      onClose={onClose}
      icon={FolderOpen}
      title={`Shared folders — ${username}`}
      subtitle={isLoading ? "Reading their share list…" : `${dirs.length} folder(s) · ${fmtSize(totalBytes)}`}
      width="max-w-3xl"
      bodyClass="p-3 space-y-2"
      headerExtra={
        <>
          <button
            className="btn-ghost !py-1 text-xs shrink-0"
            disabled={isFetching}
            onClick={() => refetch()}
            title="Re-read the share list from slskd"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} />
          </button>
          <button
            className="btn-ghost !py-1 text-xs shrink-0"
            disabled={busy !== null || picked.size === 0}
            onClick={queuePicked}
            title="Queue every file in the ticked folders"
          >
            <Download className="h-3.5 w-3.5" /> Queue selected{picked.size > 0 ? ` (${picked.size})` : ""}
          </button>
          <button
            className="btn-ghost !py-1 text-xs shrink-0"
            disabled={busy !== null}
            onClick={queueUser}
            title={`Queue everything ${username} shares — slskd scans their whole file list`}
          >
            <PackageOpen className="h-3.5 w-3.5" /> All from {username}
          </button>
        </>
      }
    >
      <input
        className="input w-full !py-1.5 text-xs"
        placeholder="Filter folders (artist, album, path…)"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      {isLoading ? (
        <PageLoading label={`Browsing ${username}'s shares…`} />
      ) : error ? (
        <EmptyState title="Could not read the share list" hint={String(error)} />
      ) : shown.length === 0 ? (
        <EmptyState
          title={dirs.length === 0 ? `${username} shares no folders` : `No folder matches “${text.trim()}”`}
          hint={dirs.length === 0
            ? "They may have sharing turned off, or slskd has not finished reading their file list yet."
            : "Clear the filter to see their whole share."}
        />
      ) : (
        <>
          <div className="space-y-2 stagger">
            {shown.slice(0, limit).map((d) => {
              const isOpen = open.has(d.directory);
              const total = d.files.reduce((n, f) => n + (f.size || 0), 0);
              return (
                <div key={d.directory} className="rounded-lg border border-border overflow-hidden">
                  <div className="flex items-center gap-2 px-3 py-2 bg-panel/60">
                    <input
                      type="checkbox"
                      className="accent-accent shrink-0"
                      checked={picked.has(d.directory)}
                      onChange={() => togglePick(d.directory)}
                      title="Tick to include this folder in “Queue selected”"
                      aria-label={`Select ${d.directory}`}
                    />
                    <button className="flex-1 min-w-0 flex items-center gap-2 text-left" onClick={() => toggle(d.directory)} title={d.directory}>
                      {isOpen
                        ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
                        : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-zinc-500" />}
                      <span className="min-w-0">
                        <span className="block text-xs text-zinc-100 truncate">
                          {d.directory.split(/[\\/]/).filter(Boolean).slice(-2).join(" / ") || d.directory}
                        </span>
                        <span className="block text-[10px] text-zinc-500">
                          {d.files.length} file(s) · {fmtSize(total)}
                        </span>
                      </span>
                    </button>
                    <button
                      className="btn-ghost !py-1 text-xs shrink-0"
                      disabled={busy !== null || d.files.length === 0}
                      onClick={() => queue(d)}
                      title="Queue every file in this folder"
                    >
                      <Download className="h-3.5 w-3.5" /> Download
                    </button>
                    <button
                      className="btn-ghost !py-1 text-xs shrink-0"
                      disabled={busy !== null}
                      onClick={() => auto(d)}
                      title="Search the release this folder holds and import it fully tagged"
                    >
                      <Zap className="h-3.5 w-3.5" /> Auto-import
                    </button>
                  </div>
                  {isOpen && (
                    <div className="border-t border-border/60 max-h-64 overflow-auto">
                      {d.files.map((f, i) => (
                        <div key={i} className="flex items-center gap-3 px-3 py-1 border-t border-border/40 first:border-t-0 text-xs">
                          <span className="flex-1 min-w-0 truncate text-zinc-300" title={f.filename}>{fileName(f.filename)}</span>
                          <span className="text-zinc-500 w-16 text-right shrink-0">{fmtSize(f.size)}</span>
                          <button
                            className="btn-ghost !px-1.5 !py-0.5 shrink-0"
                            disabled={busy !== null}
                            onClick={() => queueFile(f)}
                            title="Queue this file on its own"
                          >
                            <Download className="h-3 w-3" />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {shown.length > limit && (
            <button className="btn-secondary w-full py-2 text-xs" onClick={() => setLimit((n) => n + 200)}>
              Show more ({shown.length - limit} folders remaining)
            </button>
          )}
        </>
      )}
    </Modal>
  );
}

// Containers the browser decodes natively; the preview endpoint transcodes
// everything else (DVD VOB, Blu-ray M2TS, MPEG-2 in AVI…) live to MP4.
const NATIVE_VIDEO_EXTS = new Set(["MP4", "M4V", "WEBM", "MKV", "MOV", "OGV", "3GP", "3G2"]);

interface ReviewFile {
  path: string;
  file: string;
  ext: string;
  is_video: boolean;
  size: number;
  mtime: number;
  user: string;
  tags: Record<string, string | null>;
  tech: Record<string, number | string>;
}

/** Pull a probable artist / title / track# out of a raw download filename
 * ("01 - Artist - Title.flac", "Artist - 01 - Title.vob", …) so the tag
 * form starts from something better than a filename. */
function parseDownloadName(file: string): { artist?: string; title: string; track?: string } {
  let base = fileName(file).replace(/\.[a-z0-9]+$/i, "").replace(/_/g, " ").trim();
  let track: string | undefined;
  const tm = /^(\d{1,3})\s*[-._]?\s+(.+)$/.exec(base);
  if (tm) {
    track = tm[1];
    base = tm[2].trim();
  }
  const parts = base.split(/\s+-\s+/);
  if (parts.length >= 2) return { artist: parts[0], title: parts.slice(1).join(" - "), track };
  return { title: base, track };
}

const TAG_FIELDS: { key: string; label: string; placeholder?: string }[] = [
  { key: "TITLE", label: "Title" },
  { key: "ARTIST", label: "Artist" },
  { key: "ALBUM", label: "Album" },
  { key: "DISCNUMBER", label: "Disc", placeholder: "1" },
  { key: "TRACKNUMBER", label: "Track", placeholder: "1" },
  { key: "DATE", label: "Date", placeholder: "1997" },
  { key: "GENRE", label: "Genre" },
];

/** Every mounted preview player on this page. Windows keeps the streamed file
 *  open while a player holds it, so anything that moves or reads those files
 *  (the import, the tagging wizard) must release them first. */
const previews = new Set<HTMLMediaElement>();
const stopPreviews = () => {
  for (const m of previews) {
    m.pause();
    m.removeAttribute("src");
    m.load();
  }
  // Already released — forgetting them keeps a dead element from being
  // paused over and over on every import.
  previews.clear();
};

/** One completed download on disk: preview it (audio/video player), tag it
 *  (VOB and friends are remuxed to MKV by the tag write — stream copy, no
 *  quality or caption loss), or discard it. */
function ReviewRow({ f, onChanged }: { f: ReviewFile; onChanged: () => void }) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const [tagOpen, setTagOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [armDelete, setArmDelete] = useState(false);
  const media = useRef<HTMLMediaElement | null>(null);
  useEffect(() => {
    const el = media.current;
    return () => { if (el) previews.delete(el); };
  }, [previewOpen]);
  const parsed = useMemo(() => parseDownloadName(f.file), [f.file]);
  const [form, setForm] = useState<Record<string, string>>({
    TITLE: f.tags.TITLE ?? parsed.title ?? "",
    ARTIST: f.tags.ARTIST ?? parsed.artist ?? "",
    ALBUM: f.tags.ALBUM ?? "",
    DISCNUMBER: f.tags.DISCNUMBER ?? "",
    TRACKNUMBER: f.tags.TRACKNUMBER ?? parsed.track ?? "",
    DATE: f.tags.DATE ?? "",
    GENRE: f.tags.GENRE ?? "",
    ITUNESADVISORY: f.tags.ITUNESADVISORY ?? "0",
  });
  const set = (k: string, v: string) => setForm((m) => ({ ...m, [k]: v }));

  const save = async () => {
    setBusy(true);
    try {
      const clean: Record<string, string> = {};
      for (const [k, v] of Object.entries(form)) if (v.trim()) clean[k] = v.trim();
      if (f.is_video) {
        // a container swap (remux to MKV) announces itself from api.videoTag
        await api.videoTag(f.path, clean);
        toast("Tags written");
      } else {
        // audio downloads belong to the audio tag writer — /api/videos/tag
        // rejects anything that isn't a video container
        await api.mbAssign({ [f.path]: clean });
        toast("Tags written");
      }
      onChanged();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const discard = async () => {
    if (!armDelete) {
      setArmDelete(true);
      setTimeout(() => setArmDelete(false), 3000);
      return;
    }
    setBusy(true);
    try {
      await api.soulseekDeleteLocal(f.path);
      toast(`Discarded ${f.file}`);
      onChanged();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      setArmDelete(false);
    }
  };

  const native = NATIVE_VIDEO_EXTS.has(f.ext);
  const src = f.is_video ? api.soulseekPreviewStreamUrl(f.path) : api.soulseekLocalFileUrl(f.path);

  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 bg-panel/60">
        <button className="flex-1 min-w-0 text-left" onClick={() => setPreviewOpen(!previewOpen)} title={f.path}>
          <div className="text-xs text-zinc-100 truncate font-medium flex items-center gap-1.5">
            {f.is_video
              ? <FileVideo className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
              : <Music2 className="h-3.5 w-3.5 shrink-0 text-zinc-500" />}
            {f.file}
          </div>
          <div className="text-[10px] text-zinc-500 flex items-center gap-1.5 mt-0.5 flex-wrap">
            <span className="chip text-[9px] bg-raise border border-border">{f.ext}</span>
            {f.is_video && !native && (
              <span title="Browser can't decode this container — preview streams a live transcode (the file itself is untouched)">
                transcode preview
              </span>
            )}
            <span>· {f.user}</span>
            <span>· {fmtSize(f.size)}</span>
            {f.tech?.length ? <span>· {fmtDur(Number(f.tech.length))}</span> : null}
          </div>
        </button>
        <button
          className={`btn-ghost !py-1 text-xs shrink-0 ${previewOpen ? "!text-accent" : ""}`}
          onClick={() => setPreviewOpen(!previewOpen)}
          title={f.is_video ? "Watch (preview player)" : "Listen (preview player)"}
        >
          <Play className="h-3.5 w-3.5" /> Preview
        </button>
        <button
          className={`btn-ghost !py-1 text-xs shrink-0 ${tagOpen ? "!text-accent" : ""}`}
          onClick={() => setTagOpen(!tagOpen)}
          title="Check / edit the tags before import — video files are remuxed to MKV on save (stream copy, no quality loss)"
        >
          <Tag className="h-3.5 w-3.5" /> Tag
        </button>
        <button
          className={`btn-ghost !py-1 text-xs shrink-0 ${armDelete ? "!text-red-300 border border-red-800" : ""}`}
          disabled={busy}
          onClick={discard}
          title={armDelete ? "Click again to delete this download" : "Delete this download without importing"}
        >
          <Trash2 className="h-3.5 w-3.5" /> {armDelete ? "Sure?" : ""}
        </button>
      </div>
      {previewOpen && (
        <div className="border-t border-border/60 p-2 bg-black/30">
          {f.is_video ? (
            <video
              key={f.path}
              ref={(el) => { media.current = el; if (el) previews.add(el); }}
              controls
              autoPlay
              className="w-full max-h-72 rounded-lg bg-black"
              src={src}
            />
          ) : (
            <audio
              key={f.path}
              ref={(el) => { media.current = el; if (el) previews.add(el); }}
              controls
              autoPlay
              className="w-full"
              src={src}
            />
          )}
        </div>
      )}
      {tagOpen && (
        <div className="border-t border-border/60 p-3 space-y-2 bg-panel/40">
          <div className="grid sm:grid-cols-4 gap-2">
            {TAG_FIELDS.map((t) => (
              <label key={t.key} className="text-[10px] text-zinc-500 block">
                {t.label}
                <input
                  className="input !py-1 !px-2 text-xs mt-0.5 w-full"
                  value={form[t.key] ?? ""}
                  placeholder={t.placeholder}
                  onChange={(e) => set(t.key, e.target.value)}
                />
              </label>
            ))}
            <label className="text-[10px] text-zinc-500 block">
              Advisory
              <select
                className="input !py-1 !px-2 text-xs mt-0.5 w-full"
                value={form.ITUNESADVISORY ?? "0"}
                onChange={(e) => set("ITUNESADVISORY", e.target.value)}
              >
                <option value="0">Clean (0)</option>
                <option value="1">Explicit (1)</option>
                <option value="2">Cleaned (2)</option>
              </select>
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-primary !py-1 text-xs" disabled={busy} onClick={save}>
              <Save className="h-3.5 w-3.5" /> Save tags{f.is_video ? " (remux to MKV)" : ""}
            </button>
            <span className="text-[10px] text-zinc-600">
              {f.is_video
                ? "Video / audio / captions are stream-copied — nothing is re-encoded."
                : "Writes ID3/Vorbis/MP4 tags in place."}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

/** Album folder a review file belongs to: the shallowest folder below the
 * download dir that directly holds review files. slskd mirrors a completed
 * download as <download dir>/<remote folder>/file (older runs kept a <user>
 * level in front of it), so that is the album root for both layouts, and a
 * nested disc subfolder with files of its own belongs to the album instead of
 * becoming one — the same grouping import_completed uses. A file sitting
 * higher up than that keeps the folder it is in. */
function reviewAlbumDir(downloadDir: string, filePath: string, foldersWithFiles: Set<string>): string {
  const sep = filePath.includes("\\") ? "\\" : "/";
  const parts = filePath.split(/[\\/]/);
  const rootDepth = downloadDir.replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean).length;
  for (let d = rootDepth + 1; d < parts.length - 1; d++) {
    const dir = parts.slice(0, d).join(sep);
    if (foldersWithFiles.has(dir)) return dir;
  }
  return parts.slice(0, -1).join(sep);
}

/** How many albums the import skipped because their transfers were still
 * running (contract F). Older servers omit the key, so it is read defensively
 * rather than off a declared shape. */
function skippedAlbumCount(result: object): number {
  const raw: unknown = Reflect.get(result, "skipped");
  if (Array.isArray(raw)) return raw.length;
  return typeof raw === "number" ? raw : 0;
}

/** Review completed downloads: preview, tag (and remux VOB→MKV), discard,
 * then import the keepers into the library. Each completed album can also be
 * handed to the import wizard for the guided cover/lyrics/advisory flow. */
function ReviewPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { data, refetch } = useQuery({
    queryKey: ["soulseekReview"],
    queryFn: api.soulseekReview,
    refetchInterval: 8000,
  });
  const files = data?.files ?? [];
  const albums = useMemo(() => {
    const folders = new Set(files.map((f) => f.path.split(/[\\/]/).slice(0, -1).join(f.path.includes("\\") ? "\\" : "/")));
    const byDir = new Map<string, number>();
    for (const f of files) {
      const dir = reviewAlbumDir(data?.dir ?? "", f.path, folders);
      byDir.set(dir, (byDir.get(dir) ?? 0) + 1);
    }
    return [...byDir.entries()];
  }, [files, data?.dir]);
  const refresh = () => {
    refetch();
    qc.invalidateQueries({ queryKey: ["library"] });
  };
  const [justImported, setJustImported] = useState<{
    moved: string[];
    failed: { album: string; reason: string }[];
  } | null>(null);
  const importAll = async () => {
    // The preview player streams the file straight off disk; Windows won't let
    // the import move a file that is still open, so release every player first.
    stopPreviews();
    try {
      const r = await api.soulseekImport();
      // The moved folders are gone from the download dir by the time this panel
      // refreshes, so remember them (and what failed) until the next import.
      setJustImported({ moved: r.moved ?? [], failed: r.failed ?? [] });
      // Albums whose transfers are still running are reported separately and
      // left in the download folder; an older server omits the key entirely.
      const skipped = skippedAlbumCount(r);
      if (r.moved.length) {
        const conv = r.converted ? ` · ${r.converted} lossless file(s) converted` : "";
        toast(r.organized === false
          ? `Imported ${r.moved.length} album folder(s) — organize failed: ${r.organize_error ?? "see console"}`
          : `Imported and organized ${r.moved.length} album folder(s) into the library${conv}`);
        refresh();
      } else if (!skipped && !(r.failed ?? []).length) {
        toast("Nothing to import — no completed downloads found");
      }
      if (skipped) {
        toast(`${skipped} album(s) are still downloading — skipped for now, and they stay in the download folder until finished`);
      }
    } catch (e) {
      toast.error(String(e));
    }
  };

  return (
    <div className="panel">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
          <PackageOpen className="h-3.5 w-3.5" /> Review downloads
          {files.length > 0 && (
            <span className="text-[10px] font-mono normal-case text-zinc-400">{files.length} file(s) ready</span>
          )}
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost !py-1 text-xs" onClick={() => refetch()} title="Rescan the download folder">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
          <button className="btn-primary !py-1 text-xs" onClick={importAll} title="Ingest completed downloads into the library">
            <Download className="h-3.5 w-3.5" /> Import completed
          </button>
        </div>
      </div>
      <div className="text-[11px] text-zinc-500 mb-2.5">
        Preview each download, fix its tags (untagged DVD/Blu-ray rips included — saving remuxes them to MKV without
        re-encoding), discard the misses, then import the keepers. Files stay in{" "}
        <span className="font-mono text-zinc-400">{data?.dir ?? "…"}</span> until an import finishes — nothing is moved
        out of it before that.
      </div>
      {albums.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2.5">
          {albums.map(([dir, count]) => (
            <button
              key={dir}
              className="btn-ghost !py-1 text-xs"
              onClick={() => {
                stopPreviews();
                navigate(`/import?album=${encodeURIComponent(dir)}`);
              }}
              title={`Open the import wizard for ${dir} — covers, lyrics and advisory`}
            >
              <Tag className="h-3.5 w-3.5" /> Tag album ·{" "}
              {dir.split(/[\\/]/).filter(Boolean).pop() ?? dir}
              <span className="text-[10px] font-mono text-zinc-500 ml-1">{count}</span>
            </button>
          ))}
        </div>
      )}
      {justImported && justImported.moved.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2.5">
          {justImported.moved.map((dir) => (
            <button
              key={dir}
              className="btn-ghost !py-1 text-xs"
              onClick={() => {
                stopPreviews();
                navigate(`/import?album=${encodeURIComponent(dir)}`);
              }}
              title={`Open the import wizard for ${dir} — covers, lyrics and advisory`}
            >
              <Tag className="h-3.5 w-3.5" /> Tag album ·{" "}
              {dir.split(/[\\/]/).filter(Boolean).pop() ?? dir}
            </button>
          ))}
        </div>
      )}
      {justImported && justImported.failed.length > 0 && (
        <div className="mb-2.5 rounded-lg border border-red-800 bg-red-950/40 p-2.5">
          <div className="text-xs font-semibold text-red-300 mb-1">
            {justImported.failed.length} album(s) could not be imported
          </div>
          <div className="space-y-0.5 text-[11px] text-red-200/80">
            {justImported.failed.map((x) => (
              <div key={x.album} title={x.album}>
                <span className="text-red-300">
                  {x.album.split(/[\\/]/).filter(Boolean).pop() ?? x.album}
                </span> — {x.reason}
                {/winerror\s*32|being used by another process|in use/i.test(x.reason)
                  ? " · a file is still in use — stop the preview player and retry"
                  : ""}
              </div>
            ))}
          </div>
        </div>
      )}
      {files.length === 0 ? (
        <EmptyState
          title="Nothing waiting for review"
          hint="Finished downloads appear here automatically — slskd writes them into the download folder first."
        />
      ) : (
        <div className="space-y-2 max-h-[420px] overflow-auto pr-1 stagger">
          {files.map((f) => (
            <ReviewRow key={f.path} f={f} onChanged={refresh} />
          ))}
        </div>
      )}
    </div>
  );
}

/** Share configuration (la musica settings are the source of truth — the
 * slskd yaml is regenerated from them at start) with live rescan and the
 * autostart preference. Reserved folders (.mlo/data / .mlo/downloads /
 * .mlo/trash) are filtered server-side and never shared. */
function SharingCard({ running }: { running: boolean }) {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["soulseekShares"], queryFn: api.soulseekShares });
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [newDir, setNewDir] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (data && dirs === null) setDirs((data.dirs as string[]) ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const autostart: boolean = data?.autostart ?? true;
  const scanState: string | undefined = data?.slskd?.scanState;
  const savedDirs: string[] = data?.dirs ?? [];
  const dirty =
    dirs !== null &&
    (JSON.stringify([...dirs].sort()) !== JSON.stringify([...savedDirs].sort()));

  const save = async (autostartOverride?: boolean) => {
    setBusy(true);
    try {
      const r = await api.soulseekSharesSave(dirs ?? [], autostartOverride ?? null, true);
      toast(r.restarted ? "Shares saved — slskd restarted and rescanning" : "Shares saved");
      qc.invalidateQueries({ queryKey: ["soulseekShares"] });
      qc.invalidateQueries({ queryKey: ["soulseekStatus"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const rescan = async () => {
    setBusy(true);
    try {
      await api.soulseekSharesRescan();
      toast("Share rescan started");
      qc.invalidateQueries({ queryKey: ["soulseekShares"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleAutostart = async () => {
    setBusy(true);
    try {
      await api.soulseekSharesSave(dirs ?? [], !autostart, false);
      qc.invalidateQueries({ queryKey: ["soulseekShares"] });
      qc.invalidateQueries({ queryKey: ["soulseekStatus"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel text-xs space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-widest text-zinc-500">Sharing</span>
        {scanState && (
          <span className={`chip text-[9px] border ${scanState === "Complete" ? "bg-emerald-900/40 text-emerald-300 border-emerald-800" : "bg-raise border-border text-zinc-400"}`}>
            scan: {scanState.toLowerCase()}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2.5">
          <label className="flex items-center gap-1.5 cursor-pointer text-zinc-400" title="Start slskd automatically when the app starts">
            <input type="checkbox" checked={autostart} onChange={() => toggleAutostart()} disabled={busy} />
            Start with the app
          </label>
          <button className="btn-ghost !py-1 text-xs" onClick={rescan} disabled={busy || !running}>
            <RefreshCw className="h-3.5 w-3.5" /> Rescan
          </button>
        </div>
      </div>
      {dirs !== null && (
        <div className="space-y-1">
          {dirs.map((d) => (
            <div key={d} className="flex items-center gap-2">
              <FolderOpen className="h-3.5 w-3.5 text-zinc-600 shrink-0" />
              <span className="truncate flex-1 font-mono text-[11px] text-zinc-300" title={d}>{d}</span>
              <button
                className="text-zinc-600 hover:text-red-300 shrink-0"
                onClick={() => setDirs(dirs.filter((x) => x !== d))}
                title="Stop sharing this folder"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          <div className="flex items-center gap-2">
            <input
              className="input !py-1 flex-1 font-mono text-[11px]"
              placeholder="Add a folder to share (full path)"
              value={newDir}
              onChange={(e) => setNewDir(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && newDir.trim()) {
                  setDirs([...(dirs ?? []), newDir.trim()]);
                  setNewDir("");
                }
              }}
            />
            <button
              className="btn-ghost !py-1 text-xs"
              disabled={!newDir.trim()}
              onClick={() => {
                setDirs([...(dirs ?? []), newDir.trim()]);
                setNewDir("");
              }}
            >
              Add
            </button>
          </div>
          {dirty && (
            <button className="btn-primary !py-1 text-xs w-full" onClick={() => save()} disabled={busy}>
              {busy ? "Applying…" : "Save & apply (restarts slskd to rescan)"}
            </button>
          )}
        </div>
      )}
      <div className="text-[10px] text-zinc-600">
        Reserved folders are never shared: the hidden .mlo folder holds everything — app state in .mlo/data, downloads in .mlo/downloads and the remove-from-library bin in .mlo/trash.
      </div>
    </div>
  );
}

const MBID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** Hard stop for the search poll loop, in seconds. slskd ends a search 15s
 *  after the last peer response by default, which lands well inside this. */
const SEARCH_POLL_LIMIT_S = 180;

function timeAgo(t: number | null | undefined): string {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const WISH_STATUS: Record<Wish["status"], { label: string; cls: string; icon: typeof Star }> = {
  wanted: { label: "Wanted", cls: "bg-amber-900/40 text-amber-300 border-amber-800", icon: CircleDashed },
  searching: { label: "Searching", cls: "bg-sky-900/40 text-sky-300 border-sky-800", icon: RotateCw },
  imported: { label: "Imported", cls: "bg-emerald-900/40 text-emerald-300 border-emerald-800", icon: CheckCircle2 },
  failed: { label: "Failed", cls: "bg-red-950/60 text-red-300 border-red-900", icon: AlertTriangle },
  available: { label: "Available", cls: "bg-violet-900/40 text-violet-300 border-violet-800", icon: Star },
};

function WishRow({ w, onChanged }: { w: Wish; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(w.note);
  const [failed, setFailed] = useState(false);
  const st = WISH_STATUS[w.status] ?? WISH_STATUS.wanted;
  const Icon = st.icon;

  const search = async () => {
    setBusy(true);
    try {
      const r = await api.wishSearch(w.id);
      if (!r.ok) toast(r.error || "Already searching");
      else toast(`Searching for “${w.title}”…`);
      onChanged();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };
  const saveNote = async () => {
    try {
      await api.wishUpdate(w.id, { note });
      toast("Wish updated");
      onChanged();
    } catch (e) {
      toast.error(String(e));
    }
  };
  const remove = async () => {
    try {
      await api.wishDelete(w.id);
      toast("Wish removed");
      onChanged();
    } catch (e) {
      toast.error(String(e));
    }
  };

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-3 p-2.5">
        {!failed && w.release_mbid ? (
          <img
            src={api.artUrl(`https://coverartarchive.org/release/${w.release_mbid}/front-250`)}
            alt=""
            loading="lazy"
            onError={() => setFailed(true)}
            className="h-12 w-12 rounded-md object-cover ring-1 ring-border shrink-0"
          />
        ) : (
          <div className="h-12 w-12 rounded-md bg-raise ring-1 ring-border shrink-0 flex items-center justify-center text-zinc-700">
            <Music2 className="h-5 w-5" />
          </div>
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className={`chip text-[9px] border ${st.cls}`}>
              <Icon className={`h-3 w-3 ${w.status === "searching" ? "animate-spin" : ""}`} /> {st.label}
            </span>
            {w.attempts > 0 && <span className="text-[10px] text-zinc-600">{w.attempts} attempt(s)</span>}
            <span className="text-[10px] text-zinc-600">added {timeAgo(w.added_at)}</span>
          </div>
          <div className="text-sm text-zinc-100 truncate mt-0.5" title={w.title}>{w.title || "(unknown title)"}</div>
          <div className="text-[11px] text-zinc-500 truncate">
            {w.artist}{w.year ? ` · ${w.year}` : ""}
            {w.last_error ? <span className="text-zinc-600"> — {w.last_error}</span> : null}
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {w.status !== "imported" && (
            <button className="btn-ghost !py-1 text-xs" onClick={search} disabled={busy} title="Search Soulseek now">
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
            </button>
          )}
          {w.album_path && (
            <a className="btn-ghost !py-1 text-xs" href={`/album/${encodeURIComponent(w.album_path)}`} title="Open the imported album">
              <PackageOpen className="h-3.5 w-3.5" />
            </a>
          )}
          <a
            className="btn-ghost !py-1 text-xs"
            href={`https://musicbrainz.org/release/${w.release_mbid}`}
            target="_blank"
            rel="noreferrer"
            title="Open on MusicBrainz"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
          <button className="btn-ghost !py-1 text-xs" onClick={() => setOpen(!open)} title="Notes">
            {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          </button>
          <button className="btn-ghost !py-1 text-xs text-red-300" onClick={remove} title="Remove wish">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      {open && (
        <div className="border-t border-border/60 p-2.5 flex items-center gap-2 anim-fade">
          <input
            className="input !py-1 text-xs flex-1"
            placeholder="Note — pressings to prefer, source hints…"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button className="btn-ghost !py-1 text-xs" onClick={saveNote} disabled={note === w.note}>
            <Save className="h-3.5 w-3.5" /> Save
          </button>
        </div>
      )}
    </div>
  );
}

function WishesPanel() {
  const qc = useQueryClient();
  const { data, refetch, isLoading } = useQuery({
    queryKey: ["wishes"],
    queryFn: api.wishes,
    refetchInterval: 5000,
  });
  const [mbid, setMbid] = useState("");
  const [busy, setBusy] = useState(false);
  const worker = data?.worker;
  const wishes = data?.wishes ?? [];

  // Announce a wish that gets filled (or fails) while the app is open — the
  // worker runs on its own schedule, so nothing else would tell the user. The
  // map is seeded from the first snapshot and unseen ids are skipped, so a page
  // load neither replays history as a toast burst nor announces a wish that
  // arrived already failed.
  const prevWishStatus = useRef<Map<number, string> | null>(null);
  useEffect(() => {
    if (!data) return;
    const prev = prevWishStatus.current;
    prevWishStatus.current = new Map(data.wishes.map((w) => [w.id, w.status] as const));
    if (!prev) return;
    for (const w of data.wishes) {
      const was = prev.get(w.id);
      if (!was || was === w.status) continue;
      if (w.status === "imported") {
        toast(`Wish filled — ${w.artist ? `${w.artist} — ` : ""}${w.title || "release"}`);
      } else if (w.status === "failed") {
        toast(`Wish search failed — ${w.title || "release"}${w.last_error ? `: ${w.last_error}` : ""}`);
      }
    }
  }, [data]);

  const add = async () => {
    const id = (mbid.match(MBID_RE)?.[0] ?? "").toLowerCase();
    if (!id) {
      toast("Paste a MusicBrainz release ID or URL");
      return;
    }
    setBusy(true);
    try {
      await api.wishAdd({ release_mbid: id });
      setMbid("");
      toast.success("Added to wishes — it will be found automatically");
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const searchAll = async () => {
    setBusy(true);
    try {
      const r = await api.wishesSearchAll();
      if (!r.ok) toast(r.error || "A cycle is already running");
      else toast("Searching for all due wishes…");
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const reconcile = async () => {
    setBusy(true);
    try {
      const r = await api.wishesReconcile();
      toast(r.resolved ? `${r.resolved} wish(es) resolved from the library` : "No new matches in the library");
      qc.invalidateQueries({ queryKey: ["library"] });
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="panel text-xs">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] uppercase tracking-widest text-zinc-500">Wishes</span>
          <span className="text-zinc-500">
            Save releases now; the app re-searches Soulseek on an interval and imports them when a verified copy appears.
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button className="btn-ghost !py-1 text-xs" onClick={reconcile} disabled={busy} title="Flip wishes already present in the library">
              <CheckCircle2 className="h-3.5 w-3.5" /> Sync library
            </button>
            <button className="btn-primary !py-1 text-xs" onClick={searchAll} disabled={busy}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />} Search all now
            </button>
          </div>
        </div>
        <div className="flex items-center gap-2 mt-2 flex-wrap text-[10px] text-zinc-600">
          <span className={worker?.enabled ? "text-emerald-400" : "text-amber-400"}>
            {worker?.enabled ? "worker on" : "worker off"}
          </span>
          <span>· every {worker?.interval_hours ?? 6}h</span>
          <span>· next {worker?.next_run ? timeAgo(worker.next_run).replace("ago", "from now") : "—"}</span>
          {worker?.running && worker.current && (
            <span className="text-sky-300">· searching {worker.current}</span>
          )}
          {worker && !worker.running && worker.last_result && <span>· last: {worker.last_result}</span>}
          <span>· {openWishCount(wishes)} open</span>
        </div>
      </div>

      <div className="panel">
        <div className="flex gap-2">
          <Link2 className="h-4 w-4 text-zinc-600 self-center shrink-0" />
          <input
            className="input flex-1"
            placeholder="Paste a MusicBrainz release ID or URL to wish for it"
            value={mbid}
            onChange={(e) => setMbid(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !busy && add()}
          />
          <button className="btn-primary" onClick={add} disabled={busy || !mbid.trim()}>
            <Plus className="h-4 w-4" /> Add wish
          </button>
        </div>
        <div className="text-[11px] text-zinc-600 mt-1.5">
          Tip: open any release in the MusicBrainz browser and press “Add to wishes”, or paste its URL here.
        </div>
      </div>

      {isLoading ? (
        <PageLoading label="Loading wishes…" />
      ) : wishes.length === 0 ? (
        <EmptyState
          title="No wishes yet"
          hint="Wish for a release that isn't available on Soulseek right now — it will be imported automatically once a verified copy is found."
        />
      ) : (
        <div className="space-y-2 stagger">
          {wishes.map((w) => (
            <WishRow key={w.id} w={w} onChanged={() => refetch()} />
          ))}
        </div>
      )}

      {data?.log && data.log.length > 0 && (
        <details className="panel">
          <summary className="text-[10px] uppercase tracking-widest text-zinc-500 cursor-pointer">Wish log</summary>
          <div className="mt-2 space-y-0.5 max-h-48 overflow-auto font-mono text-[10px]">
            {data.log.slice(-40).reverse().map((l, i) => (
              <div key={i} className={l.level === "warn" ? "text-amber-400/80" : l.level === "ok" ? "text-emerald-400/80" : "text-zinc-500"}>
                {new Date(l.t * 1000).toLocaleTimeString()} — {l.msg}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function openWishCount(wishes: Wish[]) {
  return wishes.filter((w) => w.status === "wanted" || w.status === "searching" || w.status === "failed").length;
}

export default function SoulseekPage() {
  const [params] = useSearchParams();
  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ["soulseekStatus"],
    queryFn: api.soulseekStatus,
    refetchInterval: 10000,
  });
  const { data: downloads, refetch: refetchDownloads } = useQuery({
    queryKey: ["soulseekDownloads"],
    queryFn: api.soulseekDownloads,
    enabled: !!status?.running,
    refetchInterval: 3000,
  });
  // Private messages — the list lives at page level (the panel reads the same
  // query) so the Messages tab badge stays current from any tab.
  const { data: messages } = useQuery({
    queryKey: ["soulseekMessages"],
    queryFn: api.soulseekMessages,
    enabled: !!status?.running,
    refetchInterval: 5000,
  });
  const msgUnread = messages?.unread
    ?? (messages?.conversations ?? []).reduce((n, c) => n + (c?.unread ?? 0), 0);

  const running = !!status?.running;

  // Start/stop transitions poll status once a second (instead of waiting for
  // the 10s background refresh) so pressing the button feels immediate.
  const [pending, setPending] = useState<null | "start" | "stop">(null);
  useEffect(() => {
    if (!pending) return;
    const iv = setInterval(refetchStatus, 1000);
    const timeout = setTimeout(() => setPending(null), 45000);
    return () => {
      clearInterval(iv);
      clearTimeout(timeout);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending]);
  useEffect(() => {
    if (pending === "start" && running) {
      setPending(null);
      toast("slskd is up");
    }
    if (pending === "stop" && !running) {
      setPending(null);
      toast("slskd stopped");
    }
  }, [pending, running]);

  // Network ports — saved into the app settings; slskd re-reads them from
  // its generated config at every start (saving while running restarts it).
  const [listenPort, setListenPort] = useState("");
  const [webPort, setWebPort] = useState("");
  const [portsBusy, setPortsBusy] = useState(false);
  useEffect(() => {
    setListenPort(String(status?.listen_port ?? ""));
    setWebPort(String(status?.web_port ?? ""));
  }, [status?.listen_port, status?.web_port]);
  const portsChanged =
    !!status && (Number(listenPort) !== Number(status.listen_port) || Number(webPort) !== Number(status.web_port));
  const savePorts = async () => {
    setPortsBusy(true);
    try {
      await api.saveConfig({
        soulseek_listen_port: Number(listenPort) || 50000,
        soulseek_web_port: Number(webPort) || 5030,
      });
      if (running) {
        await api.soulseekRestart();
        toast(`Ports saved — slskd restarted on ${Number(listenPort)}/${Number(webPort)}`);
      } else {
        toast("Ports saved — applied at the next start");
      }
      refetchStatus();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setPortsBusy(false);
    }
  };

  const [query, setQuery] = useState("");
  const [recent, setRecent] = useState<string[]>(loadRecentSearches);
  const [browseUser, setBrowseUser] = useState<string | null>(null);
  const [searchId, setSearchId] = useState<string | null>(null);
  const [results, setResults] = useState<SlskFile[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchMeta, setSearchMeta] = useState<{ fileCount: number; responseCount: number } | null>(null);
  const [busyUser, setBusyUser] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterId>("all");
  const [codec, setCodec] = useState("");
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [visibleLimit, setVisibleLimit] = useState(60);
  const [logTest, setLogTest] = useState<Record<string, { ok: boolean; text: string } | "busy">>({});
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const autoRan = useRef(false); // ?q= handoff runs once per page visit
  const releaseParam = params.get("release") ?? undefined;

  // Page tabs — Search is the default; the badge on Downloads counts active
  // transfers so progress is visible from any tab.
  type TabId = "search" | "auto" | "wishes" | "downloads" | "messages" | "sharing" | "settings";
  const TAB_LIST: { id: TabId; label: string }[] = [
    { id: "search", label: "Search" },
    { id: "auto", label: "Auto-import" },
    { id: "wishes", label: "Wishes" },
    { id: "downloads", label: "Downloads" },
    { id: "messages", label: "Messages" },
    { id: "sharing", label: "Sharing" },
    { id: "settings", label: "Settings" },
  ];
  const [tab, setTab] = useState<TabId>(() => (releaseParam ? "auto" : "search"));
  const dlFiles = (downloads?.downloads ?? []).flatMap((u: any) =>
    (u.directories ?? []).flatMap((d: any) =>
      (d.files ?? []).map((f: any) => ({ ...f, username: u.username, dir: d.directory }))));
  const dlActive = dlFiles.filter((f: any) => f.state === "InProgress" || f.state === "Queued").length;

  /** Pre-download quality check: fetch only the .log file(s), grade them,
   * clean up — shows the Logchecker score inline on the folder row. */
  const testLogs = async (g: SlskGroup) => {
    const logs = g.files.filter((f) => extOf(f.file) === "LOG");
    if (!logs.length) return;
    setLogTest((m) => ({ ...m, [g.key]: "busy" }));
    try {
      const r = await api.soulseekTestLog(g.username, logs.map((f) => ({ filename: f.file, size: f.size })));
      const text = r.logs
        .map((l) => `${l.file}: ${l.score ?? "?"}/100${l.checksum ? ` · ${l.checksum}` : ""}`)
        .join("  ·  ");
      setLogTest((m) => ({ ...m, [g.key]: { ok: r.ok, text: `${r.ok ? "PASS" : "FAIL"} — ${text}` } }));
    } catch (e) {
      setLogTest((m) => ({ ...m, [g.key]: { ok: false, text: String(e) } }));
    }
  };

  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current);
  }, []);

  const start = async () => {
    try {
      setPending("start");
      const r = await api.soulseekStart();
      if (r.ready === false) toast("slskd is starting — coming up in a moment");
      // "slskd is up" toast fires when the status poll sees it running
    } catch (e) {
      setPending(null);
      toast.error(String(e));
    }
  };

  const stop = async () => {
    try {
      setPending("stop");
      await api.soulseekStop();
    } catch (e) {
      setPending(null);
      toast.error(String(e));
    }
  };

  const runSearch = async (q?: string) => {
    const text = (q ?? query).trim();
    if (!text) return;
    if (q) setQuery(q);
    setRecent((r) => saveRecentSearch(r, text));
    setSearching(true);
    setSearchId(null); // a stale id would make Cancel search drop the wrong query
    setResults([]);
    setSearchMeta(null);
    setVisibleLimit(60);
    try {
      const r = await api.soulseekSearch(text);
      setSearchId(r.id);
      let elapsed = 0;
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        elapsed += 2;
        try {
          const res = await api.soulseekSearchResults(r.id);
          if (res.fileCount || res.responseCount) {
            setSearchMeta({
              fileCount: Number(res.fileCount || 0),
              responseCount: Number(res.responseCount || 0),
            });
          }
          if (res.responses && res.responses.length > 0) {
            setResults(res.responses);
          }
          // slskd hands the responses over only once the search has ENDED
          // (it reports counts while running), so the poll rides out the
          // whole window — a fixed 45s cutoff dropped results from searches
          // that were still collecting.
          const isDone =
            Boolean(res.isComplete) ||
            (res.state ? res.state !== "InProgress" && (res.state.includes("Completed") || res.state.includes("TimedOut")) : false);
          if (isDone || elapsed >= SEARCH_POLL_LIMIT_S) {
            if (pollRef.current) {
              clearInterval(pollRef.current);
              pollRef.current = null;
            }
            setSearching(false);
            if (res.responses && res.responses.length > 0) {
              setResults(res.responses);
            } else if (!isDone) {
              toast("The search did not finish — try again or narrow the query");
            }
          }
        } catch {
          if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
          setSearching(false);
        }
      }, 2000);
    } catch (e) {
      toast.error(String(e));
      setSearching(false);
    }
  };

  /** Stop waiting on a running search: drop it server-side and kill the poll.
   *  Before slskd hands back an id there is nothing to cancel, so the poll is
   *  all we can stop. */
  const cancelSearch = async () => {
    const id = searchId;
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setSearching(false);
    if (!id) return;
    try {
      await api.soulseekSearchCancel(id);
      toast("Search cancelled");
    } catch (e) {
      toast.error(String(e));
    }
  };

  // MusicBrainz browser handoff: /soulseek?q=… pre-fills and fires a search
  // once slskd is confirmed running (retried via the status poll otherwise).
  const handoff = params.get("q");
  useEffect(() => {
    if (!handoff || autoRan.current) return;
    if (status?.running) {
      autoRan.current = true;
      runSearch(handoff);
    } else if (status && !status.running) {
      autoRan.current = true;
      setQuery(handoff);
      toast("Start slskd to run the search");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handoff, status?.running]);

  const downloadFile = async (f: SlskFile, group: boolean) => {
    // Lossless is the expectation; a lossy-only folder is a deliberate
    // downgrade, so it is confirmed rather than silently queued.
    const folder = groups.find(
      (g) => g.username === f.username && g.files.some((x) => x.file === f.file)
    );
    if (folder && !folder.lossless) {
      const what = folder.format || "lossy audio";
      if (!window.confirm(
        `This folder has no lossless audio (${what}).\n\n` +
        `Download it anyway? Lossless copies are preferred.`
      )) return;
    }
    setBusyUser(f.username);
    try {
      let files = [{ filename: f.file, size: f.size }];
      if (group) {
        // grab every audio file in the folder (logs/cues come along server-side
        // via the remote directory listing; here we queue what we can see)
        const dir = dirName(f.file);
        const siblings = results.filter((r) => r.username === f.username && dirName(r.file) === dir);
        files = siblings.map((s) => ({ filename: s.file, size: s.size }));
      }
      const r = await api.soulseekDownload(f.username, files);
      toast.success(`Queued ${r.queued} file(s) from ${f.username}`);
      refetchDownloads();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusyUser(null);
    }
  };

  const groups = useMemo(() => groupResults(results), [results]);
  useEffect(() => {
    if (groups.length > 0) {
      // Auto-expand the best-match group on new results
      setOpenGroups((prev) => (prev.size === 0 ? new Set([groups[0].key]) : prev));
    }
  }, [groups]);
  const visible = groups.filter((g) => groupMatches(g, filter, codec));
  const counts: Record<FilterId, number> = {
    all: groups.length,
    cdrip: groups.filter((g) => g.isCdRip).length,
    lossless: groups.filter((g) => g.lossless).length,
    lossy: groups.filter((g) => !g.lossless).length,
  };
  // Every audio codec the current results actually contain, so the codec
  // filter never offers an empty choice.
  const codecs = useMemo(
    () =>
      [...new Set(results.flatMap((f) => {
        const e = (f.ext || extOf(f.file)).toUpperCase();
        return AUDIO_EXTS[e] ? [e] : [];
      }))].sort(),
    [results]
  );
  const toggleGroup = (key: string) =>
    setOpenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const expandAll = () => setOpenGroups(new Set(visible.map((g) => g.key)));
  const collapseAll = () => setOpenGroups(new Set());

  if (status && !status.installed) {
    return (
      <div className="p-6 space-y-5 mx-auto max-w-6xl">
        <PageHeader icon={ArrowDownUp} title="Soulseek" subtitle="Managed slskd" />
        <EmptyState title="slskd is not installed" hint="Install it from Settings → Dependencies (key: slskd), then reload this page." />
      </div>
    );
  }

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={ArrowDownUp}
        title="Soulseek"
        subtitle={
          <>
            Managed slskd · {pending === "start" ? (
              <span className="text-amber-300">starting…</span>
            ) : pending === "stop" ? (
              <span className="text-amber-300">stopping…</span>
            ) : status?.conflict ? (
              <span className="text-red-400">port {status?.web_port ?? ""} in use by another app</span>
            ) : running ? (
              <span className="text-emerald-400">running{status?.logged_in ? " · logged in" : status?.logged_in === false ? " · not logged in" : ""}</span>
            ) : "stopped"}
            {" · downloads: "}{status?.download_dir ?? "—"}
            {running && status?.server?.uploadSpeed != null && (
              <>
                {" · "}
                <span className="text-zinc-400">
                  ↓ {fmtRate(status.server.downloadSpeed)} · ↑ {fmtRate(status.server.uploadSpeed)}
                </span>
              </>
            )}
          </>
        }
        actions={
          <>
            {running ? (
              <button className="btn-ghost" onClick={stop} disabled={pending !== null}>
                {pending === "stop" ? "Stopping…" : <><Power className="h-4 w-4" /> Stop</>}
              </button>
            ) : (
              <button className="btn-primary" onClick={start} disabled={pending !== null}>
                {pending === "start" ? "Starting…" : <><Play className="h-4 w-4" /> Start slskd</>}
              </button>
            )}
            <button className="btn-ghost" onClick={() => { refetchStatus(); refetchDownloads(); }} title="Reload status and the transfer list">
              <RefreshCw className="h-4 w-4" />
            </button>
          </>
        }
      >
        {/* Tabs live in the header's extra row — they belong to the title. */}
        <div className="flex rounded-md border border-border overflow-x-auto max-w-full w-fit">
          {TAB_LIST.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                tab === t.id ? "bg-accent on-accent" : "bg-panel text-zinc-400 hover:text-white"
              }`}
            >
              {t.label}
              {t.id === "downloads" && dlActive > 0 ? ` · ${dlActive}` : ""}
              {t.id === "messages" && msgUnread > 0 ? ` · ${msgUnread}` : ""}
              {t.id === "search" && results.length > 0 ? ` · ${results.length}` : ""}
            </button>
          ))}
        </div>
      </PageHeader>

      {tab === "settings" && status && (
        <div className="panel flex items-center gap-2 flex-wrap text-xs">
          <span className="text-[10px] uppercase tracking-widest text-zinc-500 mr-1">Ports</span>
          <label className="flex items-center gap-1.5 text-zinc-500">
            Listen (Soulseek)
            <input
              className="input w-28 !py-1 !px-2 font-mono"
              inputMode="numeric"
              value={listenPort}
              onChange={(e) => setListenPort(e.target.value.replace(/\D/g, ""))}
              title="Port the Soulseek network sees (listen_port in slskd)"
            />
          </label>
          <label className="flex items-center gap-1.5 text-zinc-500">
            Web UI
            <input
              className="input w-28 !py-1 !px-2 font-mono"
              inputMode="numeric"
              value={webPort}
              onChange={(e) => setWebPort(e.target.value.replace(/\D/g, ""))}
              title="Local slskd API/web port"
            />
          </label>
          <button className="btn-ghost !py-1 text-xs" disabled={!portsChanged || portsBusy} onClick={savePorts}>
            <Save className="h-3.5 w-3.5" /> {portsBusy ? "Saving…" : "Save"}
          </button>
          <span className="text-[10px] text-zinc-600">
            saved in settings · {running ? "saving restarts slskd to apply" : "applied at the next start"}
          </span>
        </div>
      )}

      {tab === "sharing" && (
        <>
          <SharingCard running={running} />
          <UploadsPanel running={running} />
        </>
      )}

      {status?.conflict && (
        <PortConflictCard
          message={String(status.conflict)}
          otherUser={status.conflict_username as string | null}
        />
      )}

      {!status?.conflict && running && status?.logged_in === false && (
        status?.has_credentials && !status?.error ? (
          <ReconnectingCard
            username={String(status.username ?? "")}
            password={String(status.password ?? "")}
            onDone={refetchStatus}
          />
        ) : (
          // the daemon named its own failure (rejected password, empty
          // credentials…): stop guessing "cooldown" and show the form with
          // that reason instead
          <LoginCard
            onDone={refetchStatus}
            initialUsername={String(status?.username ?? "")}
            initialPassword={String(status?.password ?? "")}
            initialError={(status?.error as string | null) ?? null}
          />
        )
      )}

      {tab === "auto" && <AutoPanel initialMbid={releaseParam} />}

      {tab === "wishes" && <WishesPanel />}

      {tab === "search" && (
      <div className="panel-hero">
        <div className="flex flex-wrap gap-2">
          <input
            className="input flex-1"
            placeholder="Search Soulseek manually (artist — album, title, catalog #…)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !searching && runSearch()}
          />
          <button className="btn-primary" onClick={() => runSearch()} disabled={searching || !running}>
            <Search className="h-4 w-4" /> {searching ? "Searching…" : "Search"}
          </button>
          {searching && (
            <button className="btn-ghost" onClick={cancelSearch} title="Stop this search — slskd drops it and the results stop polling">
              <Square className="h-4 w-4" /> Cancel search
            </button>
          )}
        </div>
        {!query.trim() && recent.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 mt-2">
            <span className="text-[10px] uppercase tracking-widest text-zinc-600">Recent</span>
            {recent.map((q) => (
              <button
                key={q}
                className="chip px-2 py-0.5 border bg-raise border-border text-zinc-400 hover:text-white max-w-[280px] truncate"
                disabled={searching || !running}
                onClick={() => runSearch(q)}
                title={q}
              >
                {q}
              </button>
            ))}
            <button
              className="btn-ghost !px-1.5 !py-0 text-[11px] text-zinc-500"
              onClick={() => {
                setRecent([]);
                try {
                  localStorage.removeItem(RECENT_KEY);
                } catch {
                  /* storage disabled — the in-memory list is cleared regardless */
                }
              }}
              title="Clear recent searches"
            >
              ×
            </button>
          </div>
        )}
        {!running && (
          <div className="text-[11px] text-zinc-500 mt-2">Start slskd to search and download. Credentials, ports, shares and profile description live in Settings → Soulseek.</div>
        )}

        {groups.length > 0 && (
          <>
            {/* filter chips with live counts — CD rips with log+cue first */}
            <div className="flex flex-wrap items-center justify-between gap-1.5 mt-3">
              <div className="flex flex-wrap items-center gap-1.5">
                {FILTERS.map((f) => (
                  <button
                    key={f.id}
                    className={`chip px-2.5 py-1 border ${
                      filter === f.id
                        ? "bg-accent on-accent border-transparent font-semibold"
                        : "bg-raise border-border text-zinc-400 hover:text-white"
                    }`}
                    onClick={() => setFilter(f.id)}
                  >
                    {f.label} <span className={filter === f.id ? "opacity-70" : "text-zinc-600"}>{counts[f.id]}</span>
                  </button>
                ))}
                {/* codec filter, built from what the peers actually offer */}
                {codecs.length > 1 && (
                  <select
                    className="input !py-1 !w-auto text-[11px]"
                    value={codec}
                    onChange={(e) => setCodec(e.target.value)}
                    title="Show only folders holding this codec"
                  >
                    <option value="">Any codec</option>
                    {codecs.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                )}
              </div>
              <div className="flex items-center gap-1.5 text-xs text-zinc-500">
                <button className="btn-ghost !py-0.5 !px-2 text-[11px]" onClick={expandAll}>Expand all</button>
                <span>·</span>
                <button className="btn-ghost !py-0.5 !px-2 text-[11px]" onClick={collapseAll}>Collapse all</button>
              </div>
            </div>

            <div className="mt-3 space-y-2 max-h-[560px] overflow-auto pr-1 stagger">
              {visible.slice(0, visibleLimit).map((g) => {
                const open = openGroups.has(g.key);
                return (
                  <div key={g.key} className="rounded-lg border border-border overflow-hidden">
                    {/* folder header: what you'd actually download */}
                    <div className="flex items-center gap-3 px-3 py-2 bg-panel/60 flex-wrap">
                      <button
                        className="flex-1 min-w-0 text-left"
                        onClick={() => toggleGroup(g.key)}
                        title={g.dir}
                      >
                        <div className="text-sm text-zinc-100 truncate font-medium">
                          {g.dir.split(/[\\/]/).filter(Boolean).slice(-2).join(" / ") || g.dir}
                        </div>
                        <div className="text-[11px] text-zinc-500 flex items-center gap-1.5 mt-0.5 flex-wrap">
                          <span className="inline-flex items-center gap-1"><User className="h-3 w-3" /> {g.username}</span>
                          <span>· {g.files.length} file(s) · {fmtSize(g.totalSize)}</span>
                          <span>· {g.slotFree ? <span className="text-emerald-400">free slot</span> : `queue ${g.minQueue}`}</span>
                        </div>
                      </button>
                      <GroupBadges g={g} />
                      {(() => {
                        const lt = logTest[g.key];
                        if (!g.hasLog || !lt || lt === "busy") return null;
                        return (
                          <span
                            className={`chip text-[9px] border ${lt.ok ? "bg-emerald-900/50 text-emerald-300 border-emerald-800" : "bg-red-950/60 text-red-300 border-red-800"}`}
                            title={lt.text}
                          >
                            log {lt.ok ? "pass" : "fail"}
                          </span>
                        );
                      })()}
                      {g.hasLog && (
                        <button
                          className="btn-ghost !py-1 text-xs shrink-0"
                          disabled={logTest[g.key] === "busy"}
                          onClick={() => testLogs(g)}
                          title="Download only the .log file(s) and grade them before committing to the album"
                        >
                          <FileCheck2 className="h-3.5 w-3.5" /> Test logs
                        </button>
                      )}
                      <button
                        className="btn-ghost !py-1 text-xs shrink-0"
                        onClick={() => setBrowseUser(g.username)}
                        title={`Browse everything ${g.username} shares`}
                      >
                        <FolderOpen className="h-3.5 w-3.5" /> Browse
                      </button>
                      <button
                        className="btn-ghost !py-1 text-xs shrink-0"
                        disabled={busyUser === g.username || !g.files.length}
                        onClick={() => downloadFile(g.files[0], true)}
                        title="Download this whole folder"
                      >
                        <Download className="h-3.5 w-3.5" /> Folder
                      </button>
                    </div>
                    {open && (
                      <div className="border-t border-border/60">
                        {g.files.map((f, i) => (
                          <div key={i} className="flex items-center gap-3 px-3 py-1.5 border-t border-border/40 first:border-t-0 text-xs">
                            <span className="flex-1 min-w-0 truncate text-zinc-300" title={fileName(f.file)}>
                              {fileName(f.file)}
                            </span>
                            <span className="text-zinc-500 w-16 text-right shrink-0">{fmtSize(f.size)}</span>
                            <span className="text-zinc-500 w-20 text-right shrink-0">
                              {f.bitrate ? `${f.bitrate}${f.vbr ? " vbr" : ""}` : extOf(f.file) || "—"}
                            </span>
                            <span className="text-zinc-500 w-10 text-right shrink-0">{fmtDur(f.duration)}</span>
                            <button
                              className="btn-ghost !px-1.5 !py-0.5 shrink-0"
                              disabled={busyUser === g.username}
                              onClick={() => downloadFile(f, false)}
                              title="Download this file"
                            >
                              <Download className="h-3 w-3" />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
              {visible.length === 0 && (
                <EmptyState
                  title={`No ${FILTERS.find((f) => f.id === filter)?.label.toLowerCase()} folders`}
                  hint="Pick another filter above — the results below the chips are what the peers actually offered."
                />
              )}
              {visible.length > visibleLimit && (
                <button
                  className="btn-secondary w-full py-2 text-xs mt-2"
                  onClick={() => setVisibleLimit((n) => n + 60)}
                >
                  Show more ({visible.length - visibleLimit} remaining)
                </button>
              )}
            </div>
          </>
        )}
        {!searching && results.length === 0 && searchId && (
          <EmptyState
            title="No results (yet)"
            hint="Try a different query, or drop the catalog number — peers hold artist/album folders, not pressings."
          />
        )}
        {searching && results.length === 0 && (
          <div className="text-[11px] text-zinc-400 mt-3 flex items-center gap-2 flex-wrap">
            <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0" />
            {searchMeta && searchMeta.fileCount > 0 ? (
              <span>
                Searching Soulseek… found <strong className="text-zinc-200">{searchMeta.fileCount.toLocaleString()}</strong> files from <strong className="text-zinc-200">{searchMeta.responseCount}</strong> peers (aggregating…)
              </span>
            ) : (
              <span>Broadcasting query to Soulseek network…</span>
            )}
            <button className="btn-ghost !py-0.5 !px-2 text-[11px]" onClick={cancelSearch} title="Stop this search">
              Cancel search
            </button>
          </div>
        )}
      </div>
      )}

      {tab === "downloads" && (
        <>
          <ReviewPanel />
          <DownloadsPanel downloads={downloads} status={status} />
        </>
      )}

      {tab === "messages" && <MessagesPanel running={running} />}

      {browseUser && (
        <BrowseModal
          username={browseUser}
          onAuto={() => setTab("auto")}
          onClose={() => setBrowseUser(null)}
        />
      )}
    </div>
  );
}

/** slskd timestamps are UTC but may arrive without a zone marker — assume Z
 *  then, otherwise "now" and the peer's clock disagree by the local offset. */
const msgTime = (ts?: string) => {
  if (!ts) return "";
  const d = new Date(/[zZ]$|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
};

/** Soulseek private messages: the peer list on the left (unread first, then
 *  alphabetical), the open thread and its composer on the right. The
 *  conversation query is the page-level one, so the tab badge tracks it from
 *  every tab; slskd carries no list preview, so a row is just peer + unread. */
function MessagesPanel({ running }: { running: boolean }) {
  const qc = useQueryClient();
  const { data: list, isError, refetch } = useQuery({
    queryKey: ["soulseekMessages"],
    queryFn: api.soulseekMessages,
    enabled: running,
    refetchInterval: 5000,
  });
  const [open, setOpen] = useState<string | null>(null);
  const [newUser, setNewUser] = useState("");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [closing, setClosing] = useState<string | null>(null);

  const thread = useQuery({
    queryKey: ["soulseekMessages", open],
    queryFn: () => api.soulseekConversation(open as string),
    enabled: running && !!open,
    refetchInterval: 3000,
  });
  const messages: SlskMessage[] = thread.data?.messages ?? [];
  const unreadInbound = messages.filter((m) => m.direction === "In" && !m.acknowledged).length;

  // Mark the thread read once it loads, and again after every poll that brings
  // new inbound messages while it is open.
  useEffect(() => {
    if (!open || unreadInbound === 0) return;
    api.soulseekMarkRead(open)
      .then(() => qc.invalidateQueries({ queryKey: ["soulseekMessages"] }))
      .catch(() => { /* the next poll retries */ });
  }, [open, unreadInbound, qc]);

  // Newest message is what you came for — keep it in view as the thread grows.
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [open, messages.length]);

  // The just-typed peer may not exist server-side yet — show it anyway so the
  // empty thread has a heading and a composer.
  const peers: SlskConversation[] = (list?.conversations ?? []).filter((c) => c?.username);
  if (open && !peers.some((c) => c.username === open)) peers.push({ username: open });
  // Unread first, then alphabetical — slskd hands the list over unordered.
  peers.sort((a, b) => Number((b.unread ?? 0) > 0) - Number((a.unread ?? 0) > 0)
    || (a.username ?? "").localeCompare(b.username ?? ""));

  const openThread = (username: string) => {
    setOpen(username);
    setDraft("");
  };

  const send = async () => {
    const text = draft.trim();
    if (!open || !text || sending) return;
    setSending(true);
    try {
      const r = await api.soulseekSendMessage(open, text);
      setDraft(""); // draft cleared only once slskd took it
      if (r && r.sent === false) toast("Message dropped — the peer ignores or blocks you");
      await thread.refetch();
      qc.invalidateQueries({ queryKey: ["soulseekMessages"] });
    } catch (e) {
      toast.error(String(e)); // draft kept so it can be retried
    } finally {
      setSending(false);
    }
  };

  const closeConversation = async (username: string) => {
    if (!window.confirm(`Close the conversation with ${username}?\n\nslskd drops its message history.`)) return;
    setClosing(username);
    try {
      await api.soulseekCloseConversation(username);
      if (open === username) setOpen(null);
      await refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setClosing(null);
    }
  };

  return (
    <div className="panel">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
          <MessageSquare className="h-3.5 w-3.5" /> Messages
        </div>
        <div className="flex-1" />
        <input
          className="input !py-1 !px-2 text-xs w-56"
          placeholder="New message — peer username"
          value={newUser}
          onChange={(e) => setNewUser(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && newUser.trim()) {
              openThread(newUser.trim());
              setNewUser("");
            }
          }}
          title="Open (or start) a thread with this Soulseek user — sending creates it"
        />
        <button
          className="btn-ghost !py-1 text-xs"
          onClick={() => { refetch(); if (open) thread.refetch(); }}
          title="Reload the conversation list and the open thread"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      {!running ? (
        <EmptyState title="slskd is not running" hint="Private messages need the daemon — start slskd above." />
      ) : isError ? (
        <EmptyState title="Could not load conversations" hint="slskd is unreachable — this retries automatically." />
      ) : (
        <div className="flex gap-3 mt-3">
          <div className="w-56 shrink-0 rounded-lg border border-border bg-panel/60 p-1 space-y-0.5 max-h-[420px] overflow-auto stagger">
            {peers.length === 0 ? (
              <div className="text-xs text-zinc-600 p-2">No conversations yet.</div>
            ) : (
              peers.map((c) => (
                <div
                  key={c.username}
                  className={`flex items-center gap-1 rounded-md pr-1 ${
                    open === c.username ? "bg-raise" : "hover:bg-raise/60"
                  }`}
                >
                  <button
                    className="flex-1 text-left px-2 py-1.5 text-xs truncate"
                    onClick={() => openThread(c.username)}
                    title={c.username}
                  >
                    {c.username}
                  </button>
                  {(c.unread ?? 0) > 0 && (
                    <span className="chip bg-accent/15 border border-accent/30 text-accent-soft" title="Unread messages">
                      {c.unread}
                    </span>
                  )}
                  <button
                    className="btn-ghost !px-1.5 !py-0.5 text-[10px]"
                    onClick={() => closeConversation(c.username)}
                    disabled={closing === c.username}
                    title="Close this conversation (drops it from slskd)"
                  >
                    Close
                  </button>
                </div>
              ))
            )}
          </div>

          <div className="flex-1 min-w-0 flex flex-col">
            {!open ? (
              <div className="text-xs text-zinc-600 py-6 text-center">Select a conversation.</div>
            ) : (
              <>
                <div className="text-xs font-semibold text-zinc-300 truncate mb-1.5" title={open}>
                  {open}
                </div>
                <div ref={scrollRef} className="rounded-lg border border-border bg-panel/40 p-2 space-y-1.5 h-[320px] overflow-auto stagger">
                  {messages.length === 0 ? (
                    <div className="text-xs text-zinc-600 py-3 text-center">
                      No messages yet — say hello.
                    </div>
                  ) : (
                    messages.map((m, i) => {
                      const inbound = m.direction !== "Out";
                      return (
                        <div key={m.id ?? i} className={`flex ${inbound ? "justify-start" : "justify-end"}`}>
                          <div
                            className={`max-w-[75%] rounded-lg border px-2.5 py-1.5 text-xs whitespace-pre-wrap break-words ${
                              inbound
                                ? "bg-raise border-border text-zinc-200"
                                : "bg-accent/15 border-accent/25 text-zinc-100"
                            }`}
                          >
                            {m.message ?? ""}
                            <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-zinc-500">
                              <span className="font-mono">{msgTime(m.timestamp)}</span>
                              {inbound && !m.acknowledged && (
                                <span className="chip bg-amber-900/40 border border-amber-800 text-amber-300" title="Not acknowledged yet">
                                  unread
                                </span>
                              )}
                              {m.replayed && <span className="chip bg-raise border border-border text-zinc-500">replayed</span>}
                            </div>
                          </div>
                        </div>
                      );
                    })
                  )}
                </div>
                <div className="mt-2 flex gap-2">
                  <textarea
                    className="input text-xs min-h-[64px] flex-1"
                    placeholder="Message — Enter sends, Shift+Enter for a new line"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        send();
                      }
                    }}
                    disabled={sending}
                  />
                  <button
                    className="btn-primary !py-1 text-xs self-end"
                    onClick={send}
                    disabled={sending || !draft.trim()}
                  >
                    {sending ? "Sending…" : "Send"}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** States slskd never moves again — only these can be cancelled or cleared.
 *  anything else is still queued or running. */
const DONE_STATES: Record<string, true> = {
  Completed: true, Succeeded: true, Errored: true, Cancelled: true, Rejected: true,
  FileNotFound: true, Aborted: true, TimedOut: true, Failed: true,
};

/** One transfer row, flattened out of slskd's per-user directory tree. */
type TransferRow = SlskTransfer & { username: string; dir: string };

/** Transfer statuses for the Downloads tab: active transfers with progress
 * bars, plus queued / completed / failed buckets so the history is
 * browsable instead of one flat list. Cancel drops a queued/running transfer,
 * Retry re-queues a failed one, and "Clear finished" empties the history. */
function DownloadsPanel({ downloads, status }: {
  downloads: SlskDownloads | undefined;
  /** daemon state — an empty list means different things running vs stopped */
  status: { running?: boolean; installed?: boolean } | undefined;
}) {
  const qc = useQueryClient();
  const [view, setView] = useState<"active" | "completed">("active");
  const [busy, setBusy] = useState<string | null>(null);
  const files: TransferRow[] = (downloads?.downloads ?? []).flatMap((u) =>
    u.directories.flatMap((d) => d.files.map((f) => ({ ...f, username: u.username, dir: d.directory }))));
  const pct = (f: SlskTransfer) => {
    if (typeof f.percentComplete === "number") return Math.round(f.percentComplete);
    if (f.size) return Math.min(100, Math.round(((f.bytesTransferred ?? 0) / f.size) * 100));
    return 0;
  };
  // slskd reports transfer states as compound strings — "Completed,
  // Succeeded", "InProgress, Errored" — so every bucket matches on the
  // CONTAINED token, never on equality (an exact "Completed" match found
  // nothing and the history always read 0).
  const has = (f: SlskTransfer, token: string) => f.state.includes(token);
  const done = (s: string) => Object.keys(DONE_STATES).some((k) => s.includes(k));
  const active = files.filter((f) => has(f, "InProgress"));
  const queued = files.filter((f) => has(f, "Queued") || has(f, "Requested") || has(f, "Initializing"));
  const completed = files.filter((f) => has(f, "Succeeded"));
  const failed = files.filter(
    (f) => !completed.includes(f) && !active.includes(f) && !queued.includes(f) && done(f.state));
  const finished = completed.length + failed.length;
  const shown = view === "active" ? [...active, ...queued] : [...completed, ...failed];
  const bucket = (f: TransferRow) =>
    has(f, "Succeeded") ? (
      <span className="chip text-[9px] bg-emerald-900/40 text-emerald-300 border border-emerald-800">done</span>
    ) : has(f, "InProgress") ? (
      <span className="chip text-[9px] bg-sky-900/40 text-sky-300 border border-sky-800">{pct(f)}%</span>
    ) : has(f, "Queued") ? (
      <span className="chip text-[9px] bg-raise border border-border text-zinc-400">queued</span>
    ) : (
      <span className="chip text-[9px] bg-red-950/60 text-red-300 border border-red-900">{f.state.toLowerCase() || "failed"}</span>
    );

  /** Cancel one or many transfers — slskd takes a transfer_id list, but only
   *  per user, so a mixed bucket is one request per peer. */
  const cancel = async (rows: TransferRow[]) => {
    if (rows.length === 0) return;
    setBusy("*");
    try {
      const byUser = new Map<string, string[]>();
      for (const f of rows) byUser.set(f.username, [...(byUser.get(f.username) ?? []), f.id]);
      const answers = await Promise.all(
        [...byUser].map(([user, ids]) => api.soulseekDownloadsCancel(user, ids))
      );
      const n = answers.reduce((a, r) => a + r.cancelled, 0);
      toast(
        n === 0
          ? "Could not cancel those transfers"
          : rows.length === 1
            ? `Cancelled ${fileName(rows[0].filename)}`
            : `Cancelled ${n} transfer(s)`
      );
      qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  /** Same call as a fresh queue — slskd starts the file again from scratch. */
  const retry = async (rows: TransferRow[]) => {
    if (rows.length === 0) return;
    setBusy("*");
    try {
      const byUser = new Map<string, { filename: string; size: number }[]>();
      for (const f of rows) {
        byUser.set(f.username, [...(byUser.get(f.username) ?? []), { filename: f.filename, size: f.size }]);
      }
      await Promise.all([...byUser].map(([user, list]) => api.soulseekDownload(user, list)));
      toast(
        rows.length === 1
          ? `Retrying ${fileName(rows[0].filename)}`
          : `Retrying ${rows.length} failed transfer(s)`
      );
      qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const clearFinished = async () => {
    setBusy("*");
    try {
      const r = await api.soulseekDownloadsClear();
      toast(r.cleared ? `Cleared ${r.cleared} finished transfer(s)` : "Nothing to clear");
      qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="panel">
      <div className="flex items-center justify-between gap-2 flex-wrap mb-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Downloads</div>
        <div className="flex items-center gap-1 flex-wrap text-[10px]">
          {([["active", active.length], ["queued", queued.length], ["completed", completed.length], ["failed", failed.length]] as [string, number][]).map(([label, count]) => (
            <span key={label} className={`chip text-[9px] border ${count > 0 ? "bg-raise border-border text-zinc-300" : "bg-panel border-border/60 text-zinc-600"}`}>
              {label} {count}
            </span>
          ))}
          {finished > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px] ml-1"
              disabled={busy !== null}
              onClick={clearFinished}
              title="Remove finished / failed transfers from this list (in-progress and queued transfers are kept)"
            >
              <Trash2 className="h-3 w-3" /> Clear finished
            </button>
          )}
          {active.length + queued.length > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px]"
              disabled={busy !== null}
              onClick={() => cancel([...active, ...queued])}
              title="Drop every running and queued transfer from slskd's queue"
            >
              <Square className="h-3 w-3" /> Cancel all active
            </button>
          )}
          {failed.length > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px]"
              disabled={busy !== null}
              onClick={() => retry(failed)}
              title="Queue every failed transfer's file again"
            >
              <RotateCw className="h-3 w-3" /> Retry all failed
            </button>
          )}
        </div>
      </div>
      {files.length === 0 ? (
        // A stopped daemon has no queue at all — "No downloads queued." there
        // reads as "your queue is empty", which is not what happened.
        !status?.installed ? (
          <EmptyState
            title="slskd is not installed"
            hint="Install it from Settings → Dependencies (key: slskd) — downloads and transfers need the daemon."
          />
        ) : !status?.running ? (
          <EmptyState
            title="slskd is not running"
            hint="Start slskd above — queued transfers resume and finished ones stay in the history."
          />
        ) : (
          <EmptyState
            title="No downloads queued"
            hint="Search for an album and queue a folder (or browse a peer's share) — transfers appear here with live progress."
          />
        )
      ) : (
        <>
          <div className="flex rounded-md border border-border overflow-hidden w-fit mb-2">
            {(["active", "completed"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-2.5 py-1 text-[11px] font-medium transition-colors ${
                  view === v ? "bg-accent on-accent" : "bg-panel text-zinc-400 hover:text-white"
                }`}
              >
                {v === "active" ? `Active (${active.length + queued.length})` : `History (${completed.length + failed.length})`}
              </button>
            ))}
          </div>
          <div className="space-y-1 max-h-[360px] overflow-auto stagger">
            {shown.map((f, i) => (
              <div key={f.id || `${f.username}-${i}`} className="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-white/[0.04] text-xs">
                <div className="flex-1 min-w-0">
                  <div className="truncate text-zinc-200" title={f.filename}>{fileName(f.filename ?? "")}</div>
                  <div className="text-[10px] text-zinc-600 truncate" title={f.dir}>{f.username} · {f.dir}</div>
                </div>
                {view === "active" && (
                  <div className="w-16 sm:w-28 shrink-0 h-1.5 rounded-sm bg-border/70 overflow-hidden">
                    <div className={`h-full ${f.state === "InProgress" ? "bg-accent" : "bg-zinc-600"}`} style={{ width: `${pct(f)}%` }} />
                  </div>
                )}
                <span className="text-zinc-500 w-16 text-right shrink-0">{fmtSize(f.size ?? 0)}</span>
                {/* slskd reports the running average per transfer — hidden on
                    phones, where the row has no width to spare */}
                <span className="text-zinc-500 w-20 text-right shrink-0 hidden sm:block" title="Average transfer rate">
                  {fmtRate(f.averageSpeed)}
                </span>
                <span className="w-16 text-right shrink-0">{bucket(f)}</span>
                {view === "active" ? (
                  <button
                    className="btn-ghost !px-1.5 !py-0.5 text-[11px] shrink-0 w-16 justify-center"
                    disabled={busy !== null}
                    onClick={() => cancel([f])}
                    title="Drop this transfer from slskd's queue"
                  >
                    <Square className="h-3 w-3" /> Cancel
                  </button>
                ) : failed.includes(f) ? (
                  <button
                    className="btn-ghost !px-1.5 !py-0.5 text-[11px] shrink-0 w-16 justify-center"
                    disabled={busy !== null}
                    onClick={() => retry([f])}
                    title="Queue this file again"
                  >
                    <RotateCw className="h-3 w-3" /> Retry
                  </button>
                ) : (
                  <span className="w-16 shrink-0" />
                )}
              </div>
            ))}
            {shown.length === 0 && (
              <EmptyState
                title="Nothing here"
                hint={view === "active"
                  ? "No transfer is running or queued right now."
                  : "No transfer has finished or failed yet."}
              />
            )}
          </div>
        </>
      )}
    </div>
  );
}

/** Shared history: what other users are / were downloading from you —
 * live uploads plus everything completed, from slskd's upload transfers. */
function UploadsPanel({ running }: { running: boolean }) {
  const { data } = useQuery({
    queryKey: ["soulseekUploads"],
    queryFn: api.soulseekUploads,
    enabled: running,
    refetchInterval: 5000,
  });
  const files = ((data?.uploads ?? []) as any[]).flatMap((u: any) =>
    (u.directories ?? []).flatMap((d: any) =>
      (d.files ?? []).map((f: any) => ({ ...f, username: u.username, dir: d.directory }))));
  const sharingNow = files.filter((f: any) => f.state === "InProgress");
  const past = files.filter((f: any) => f.state !== "InProgress");
  const totalGiven = past.reduce((n: number, f: any) => n + (f.bytesTransferred ?? f.size ?? 0), 0);

  return (
    <div className="panel">
      <div className="flex items-center justify-between gap-2 flex-wrap mb-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Shared history (uploads)</div>
        {files.length > 0 && (
          <div className="flex gap-1 text-[10px]">
            <span className="chip text-[9px] bg-sky-900/40 text-sky-300 border border-sky-800">sharing now {sharingNow.length}</span>
            <span className="chip text-[9px] bg-raise border border-border text-zinc-300">past {past.length}</span>
            <span className="chip text-[9px] bg-panel border-border/60 text-zinc-500">{fmtSize(totalGiven)} given</span>
          </div>
        )}
      </div>
      {!running ? (
        <EmptyState title="slskd is not running" hint="Start it above to share your library." />
      ) : files.length === 0 ? (
        <EmptyState title="No uploads yet" hint="No one has pulled from your shares since slskd last started." />
      ) : (
        <div className="space-y-1 max-h-[300px] overflow-auto stagger">
          {[...sharingNow, ...past].slice(0, 60).map((f: any, i: number) => (
            <div key={`${f.username}-${i}`} className="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-white/[0.04] text-xs">
              <div className="flex-1 min-w-0">
                <div className="truncate text-zinc-200" title={f.filename}>{fileName(f.filename ?? "")}</div>
                <div className="text-[10px] text-zinc-600 truncate">{f.username}</div>
              </div>
              <span className="text-zinc-500 w-16 text-right shrink-0">{fmtSize(f.bytesTransferred ?? f.size ?? 0)}</span>
              <span className={`w-20 text-right shrink-0 chip text-[9px] border ${f.state === "InProgress" ? "bg-sky-900/40 text-sky-300 border-sky-800" : f.state === "Completed" ? "bg-emerald-900/40 text-emerald-300 border-emerald-800" : "bg-raise border-border text-zinc-400"}`}>
                {f.state === "InProgress" ? "sharing" : f.state?.toLowerCase()}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
