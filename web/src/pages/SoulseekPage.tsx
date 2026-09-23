import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownUp, ArrowDown, ArrowDownToLine, Disc3, Eye, EyeOff, FolderInput, FolderOpen, Loader2, Play, Power, RefreshCw, Search,
  User, Zap, Square, FileCheck2, FileVideo, Music2, Save, Tag, Trash2, PackageOpen,
  Star, Plus, CheckCircle2, CircleDashed, Clock, CheckSquare, AlertTriangle, ExternalLink, RotateCw, ChevronDown, ChevronRight, Link2,
  MessageSquare, X, Wand2, CheckCheck, MessageCircleQuestion, Layers,
} from "lucide-react";
import { api } from "../api";
import type { ImportRunStatus, ReadyAlbum, SlskAutoFile, SlskAutoJob, SlskStatus, SlskAutoProgress, SlskConversation, SlskDownloads, SlskMessage, SlskPortCheck, SlskQueueItem, SlskQueuePayload, SlskQueueScope, SlskSearchProgress, SlskTransfer, StagingEntry, StagingRoot, StagingRootId } from "../api";
import { toast } from "../store";
import { useLiveTransfers, type TransfersFrame } from "../lib/notifications";
import { SOULSEEK_QUEUE_KEY as QUEUE_KEY, STAGE_LABEL } from "../lib/acquisition";
import { EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import Modal from "../components/Modal";
import ConfirmButton from "../components/ConfirmButton";
import Segmented from "../components/Segmented";
import type { DownloadEntry, ImportBulkJob, SlskReleaseIdentity } from "../types";
import { fmtCount, fmtCounts, fmtPercent } from "../lib/fmt";
import { useI18n } from "../lib/i18n";

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
  // Seconds everywhere (slskd's remainingTime, track lengths): mm:ss, with the
  // hour field only once a long download's ETA actually needs it.
  const h = Math.floor(s / 3600);
  const m = Math.floor(s / 60) % 60;
  const sec = String(Math.floor(s % 60)).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
};
const fileName = (p: string) => p.replace(/^.*[\\/]/, "");
/** A WAIT, in the units a person reads it in: "42s" / "3m 07s" / "1h 12m" —
 *  the shape a countdown needs, without a clock the user has to subtract from.
 *  Units only: the sentence around it is the translated one (`queue.wait_*`). */
const fmtWait = (s: number) => {
  const t = Math.max(0, Math.floor(s));
  if (t < 60) return `${t}s`;
  if (t < 3600) return `${Math.floor(t / 60)}m ${String(t % 60).padStart(2, "0")}s`;
  return `${Math.floor(t / 3600)}h ${String(Math.floor((t % 3600) / 60)).padStart(2, "0")}m`;
};
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

/** One pasted MusicBrainz reference: the MBID and the ENTITY it names.
 *
 *  A URL says which entity outright (`/release-group/…`, `/artist/…`,
 *  `/recording/…`, `/release/…`); a bare MBID is a UUID with no type in it, so
 *  it goes out as `auto` and the SERVER detects the entity
 *  (`server.integrations._kind_for` is what the MusicBrainz pages already rely
 *  on), so the two paths cannot disagree about what a pasted id is. Anything
 *  else is no reference at all — a link to a label, a work or a place is named
 *  as unlookable rather than having its UUID read as a release, which is what
 *  the old release-only parser did. */
type MbKind = "release" | "release_group" | "artist" | "recording" | "auto";

const MB_KIND_BY_PATH: Record<string, MbKind> = {
  "release": "release", "release-group": "release_group",
  "artist": "artist", "recording": "recording",
};

function mbRef(input: string): { mbid: string; kind: MbKind } | null {
  const raw = String(input || "").trim();
  if (!raw) return null;
  const url = /musicbrainz\.org\/(release-group|release|artist|recording)\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(raw);
  if (url) {
    return { mbid: url[2].toLowerCase(), kind: MB_KIND_BY_PATH[url[1].toLowerCase()] ?? "auto" };
  }
  const bare = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.exec(raw);
  return bare ? { mbid: bare[0].toLowerCase(), kind: "auto" } : null;
}

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
function ReconnectingCard({ username, password, error, onDone }: {
  username: string;
  password: string;
  error?: string | null;
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
      {error ? (
        <span className="text-red-300">slskd said: {error}</span>
      ) : (
        <span className="text-zinc-600">
          the network sometimes enforces a short cooldown after a disconnect
        </span>
      )}
      <div className="ml-auto flex flex-wrap gap-1.5 justify-end">
        <button className="btn-ghost !py-1 text-xs tap" onClick={retry} disabled={busy}>
          {busy ? "Reconnecting…" : "Reconnect now"}
        </button>
        <button className="btn-ghost !py-1 text-xs tap" onClick={() => setShowForm(true)}>
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
      <a className="btn-ghost text-xs tap" href="/settings">
        <ExternalLink className="h-3.5 w-3.5" /> Open Settings
      </a>
    </div>
  );
}

/** The LISTEN port held by a FOREIGN program — the same treatment the web
 *  port's own conflict gets above, because it is the same kind of fact: another
 *  program has the port, so slskd cannot use it. Without it the only symptom is
 *  peers quietly failing to reach this app. */
function ListenPortConflict({ message, port }: { message: string; port: number }) {
  return (
    <div className="panel border-red-900/50">
      <div className="text-xs font-semibold uppercase tracking-wider text-red-300 mb-1.5 flex items-center gap-1.5">
        <AlertTriangle className="h-3.5 w-3.5" /> Soulseek listen port already in use
      </div>
      <p className="text-[11px] text-zinc-400 mb-2.5">{message}.</p>
      <p className="text-[11px] text-zinc-500">
        Port {port || "…"} is the one peers connect to, so while that program holds it nobody can
        reach your shares (transfers still work from here, because this app starts them itself).
        Quit that program, or set another listen port in Settings → Soulseek and save it. This app
        never stops another program for you.
      </p>
    </div>
  );
}

/** The LISTEN port's real state, as the server measured it (mlo.portmap): who
 *  accepts on it, what the ROUTER was actually told, and — when it is not open
 *  — the endpoint's or the daemon's own words for why.
 *
 *  Nothing here is rounded up. A mapping reads "opened" only when a router
 *  confirmed it; one the router merely accepted without letting us read the
 *  entry back says exactly that; a refusal carries the router's own message
 *  instead of a guess at its cause. That honesty is the whole point — peers
 *  failing to connect is otherwise the only symptom this state has. */
function ListenPortState({ state }: { state: SlskStatus["listen_port_state"] }) {
  if (!state) return null;
  const m = state.mapping;
  const port = state.listen_port || m?.mapped_port || 0;
  const mapped = m?.mapped_port || port;
  const method = m?.method === "natpmp" ? "NAT-PMP" : m?.method === "upnp" ? "UPnP" : "";
  // Both outcomes of the probe, when both were tried (UPnP answered nothing,
  // NAT-PMP mapped it): the message the user has to act on is the pair.
  const tried = (m?.tried ?? [])
    .map((t) => `${(t.method || "").toUpperCase()}: ${t.state}${t.detail ? ` — ${t.detail}` : ""}`)
    .join(" · ");
  const detail = [m?.detail, tried].filter(Boolean).join(" · ");
  let label = "";
  let cls = "bg-zinc-800/70 text-zinc-400 border-zinc-700";
  switch (m?.state) {
    case "mapped":
      if (m.verified) {
        label = `opened · external ${mapped}${m.external_ip ? ` · ${m.external_ip}` : ""}${method ? ` (${method})` : ""}`;
        cls = "bg-emerald-900/40 text-emerald-300 border-emerald-800";
      } else {
        // The router took the request and cannot read the entry back: an
        // acceptance, never "open".
        label = `accepted by the router (not confirmed) · external ${mapped}${method ? ` (${method})` : ""}`;
        cls = "bg-amber-900/40 text-amber-300 border-amber-800";
      }
      break;
    case "refused":
      label = `the router refused: ${m.detail || "no reason given"}`;
      cls = "bg-amber-900/40 text-amber-300 border-amber-800";
      break;
    case "no_gateway":
      label = `no UPnP or NAT-PMP gateway answered — forward port ${mapped} on the router yourself`;
      break;
    case "unsupported":
      label = `the gateway offers no port mapping — forward port ${mapped} on the router yourself`;
      break;
    case "off":
      label = `automatic port opening is off — forward port ${port} on the router yourself`;
      break;
    case "pending":
      label = "port opening: not asked yet";
      break;
    case "checking":
      label = "asking the router…";
      break;
    case "client_down":
      label = "slskd is not running, so nothing is mapped";
      break;
    case "error":
      label = m.detail || "the port mapping check failed";
      cls = "bg-amber-900/40 text-amber-300 border-amber-800";
      break;
    default:
      label = "";
  }
  // What the mapping cannot say: the port is unbound while slskd runs, or it
  // cannot be held here at all (the server's own sentences), plus slskd's own
  // last word about a listen port when it said one.
  const fact = [state.error, state.slskd_error].filter(Boolean).join(" · ");
  if (!label && !fact) return null;
  return (
    <span className="inline-flex items-center gap-1.5 flex-wrap min-w-0">
      {label && (
        <span className={`chip text-[9px] border ${cls}`} title={detail || label}>{label}</span>
      )}
      {fact && (
        <span className="text-[10px] text-amber-300" title={[fact, detail].filter(Boolean).join(" · ")}>
          {fact}
        </span>
      )}
    </span>
  );
}

/** The port check's rows, as the server proved them (server/soulseek_port.py).

 *  Every row is one thing that CAN be observed from here, and each carries the
 *  server's own "what this proves / what it cannot" as its tooltip: a green
 *  listener is not a promise that the internet reaches the port. The note under
 *  the rows is the one thing no row can say — the outside half of the answer
 *  needs a probe from outside this network, which this app does not ship. */
const PORT_STATE_TONE: Record<string, string> = {
  ok: "bg-emerald-900/40 text-emerald-300 border-emerald-800",
  warn: "bg-amber-900/40 text-amber-300 border-amber-800",
  fail: "bg-red-900/40 text-red-300 border-red-800",
  unknown: "bg-zinc-800/70 text-zinc-400 border-zinc-700",
};

/** What the verdict means, in the page's own words: the state names alone read
 *  as a promise about the internet, which no row here can make. */
const PORT_VERDICT: Record<string, string> = {
  ok: "the port answers here and the router lists a mapping for it",
  warn: "something needs a look — the rows say which",
  fail: "the port is not reachable as configured — the failing rows say why",
  unknown: "not proven from this machine",
};

function PortCheckPanel({ result, onHide }: { result: SlskPortCheck; onHide: () => void }) {
  return (
    <div className="panel text-xs space-y-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-widest text-zinc-500">Port check</span>
        <span className={`chip text-[9px] border ${PORT_STATE_TONE[result.verdict] ?? PORT_STATE_TONE.unknown}`}>
          port {result.port}
        </span>
        <span className="text-[11px] text-zinc-400">{PORT_VERDICT[result.verdict] ?? result.verdict}</span>
        <span className="ml-auto text-[10px] text-zinc-600">
          {String(result.checked_at ?? "").slice(0, 16).replace("T", " ")}
        </span>
        <button className="text-zinc-600 hover:text-white tap" onClick={onHide} title="Hide the port check">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      {result.checks.map((c) => (
        <div key={c.id} className="flex items-start gap-2">
          <span
            className={`chip text-[9px] border shrink-0 ${PORT_STATE_TONE[c.state] ?? PORT_STATE_TONE.unknown}`}
            title={`proves: ${c.proves}\ncannot: ${c.cannot}`}
          >
            {c.state}
          </span>
          <div className="min-w-0">
            <div className="text-[11px] text-zinc-300">{c.label}</div>
            <div className="text-[10px] text-zinc-500">{c.detail}</div>
          </div>
        </div>
      ))}
      <div className="text-[10px] text-zinc-600">{result.note}</div>
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
          className="input w-full sm:w-52 tap"
          placeholder="Soulseek username"
          value={username}
          autoComplete="username"
          onChange={(e) => setUsername(e.target.value)}
        />
        <div className="relative">
          <input
            className="input w-full sm:w-52 pr-9 tap"
            placeholder="Password"
            type={showPw ? "text" : "password"}
            value={password}
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !busy && login()}
          />
          <button
            className="absolute right-2 top-1/2 -translate-y-1/2 p-[9px] -mx-[9px] text-zinc-500 hover:text-zinc-200"
            onClick={() => setShowPw(!showPw)}
            title={showPw ? "Hide password" : "Show password"}
            type="button"
          >
            {showPw ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
          </button>
        </div>
        <button className="btn-primary tap" onClick={login} disabled={busy}>
          {busy ? "Connecting…" : "Log in / create account"}
        </button>
      </div>
      {result && (
        <div className={`text-[11px] mt-2 ${failed ? "text-red-300" : "text-zinc-400"}`}>{result}</div>
      )}
    </div>
  );
}

/** Live view of the auto-import search stage: which query is out and how much
 *  the network has answered. Deliberately indeterminate — slskd's window is a
 *  ceiling a good candidate ends early, so a countdown or a filling bar
 *  promised a deadline that does not exist. */
function SearchProgress({ s }: { s: SlskSearchProgress }) {
  return (
    <div className="mt-2 flex items-center gap-2 text-[11px] text-zinc-500">
      <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent-soft" />
      <span className="min-w-0 truncate" title={s.query}>searching “{s.query}”</span>
      <span className="ml-auto shrink-0 text-zinc-400">
        {s.responses} responses · {s.files} files
      </span>
    </div>
  );
}

/** One file of the running download — the same row markup the Downloads tab
 *  uses, so both views of a transfer read alike. */
function ProgressFileRow({ f }: { f: SlskAutoFile }) {
  // Server-side fractional completion: the bar tracks the byte share slskd
  // reports at one decimal, so a big file moves between two whole percents
  // instead of appearing frozen — and the two flags are read as they come
  // rather than re-derived from the state string (a queued file that has been
  // accepted earlier would read as done forever).
  const p = Math.max(0, Math.min(100, Number(f.percent ?? 0)));
  const complete = f.complete === true; // slskd says the transfer finished
  const arrived = f.done === true; // the pipeline accepted it onto disk
  return (
    <div className="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-white/[0.04] text-xs">
      <div className="flex-1 min-w-0 truncate text-zinc-200" title={f.name}>{fileName(f.name ?? "")}</div>
      {/* The bar and the byte pair are the first things to go on a phone: the
          percentage chip in the next column already carries the same reading. */}
      <div className="hidden sm:block w-28 shrink-0 h-1.5 rounded-sm bg-border/70 overflow-hidden">
        <div className={`h-full ${complete ? "bg-emerald-500" : "bg-accent"}`} style={{ width: `${p}%` }} />
      </div>
      <span className="text-zinc-500 w-14 sm:w-24 text-right shrink-0">
        {fmtSize(f.bytes ?? 0)} / {fmtSize(f.size ?? 0)}
      </span>
      <span className="w-16 text-right shrink-0">
        {arrived ? (
          <span className="chip text-[9px] bg-emerald-900/40 text-emerald-300 border border-emerald-800">arrived</span>
        ) : complete ? (
          <span className="chip text-[9px] bg-emerald-900/40 text-emerald-300 border border-emerald-800">complete</span>
        ) : p > 0 ? (
          <span className="chip text-[9px] bg-sky-900/40 text-sky-300 border border-sky-800">{fmtPercent(p)}</span>
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
  // Two file counts sit side by side and mean different things: `files_done` is
  // slskd's own view of the transfers, `files_arrived` is what the pipeline has
  // accepted onto disk. The percentage is neither — it is byte-weighted — so
  // each gets its own label instead of reading as one measure.
  const complete = p.files_done ?? 0;
  const arrived = p.files_arrived ?? 0;
  const total = p.files_total ?? 0;
  // Server percentage when present, derived from bytes otherwise — clamped either way.
  const raw = p.percent ?? (p.size ? (100 * (p.bytes ?? 0)) / p.size : 0);
  const pct = Math.max(0, Math.min(100, Number(raw) || 0));
  const files = p.files ?? [];
  const shown = files.slice(0, 8);
  return (
    <div className="mt-2 rounded-lg border border-border bg-panel/60 p-2.5">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="font-medium text-zinc-300">{p.phase || "download"}</span>
        {p.username && <span className="text-zinc-500 min-w-0 truncate" title={p.dir}>· {p.username}</span>}
        <span className="ml-auto shrink-0 text-zinc-400">
          {fmtCounts(complete, total)} files complete · {arrived} arrived · {fmtPercent(pct)} of bytes
        </span>
      </div>
      <div className="mt-1 h-1.5 rounded-sm bg-border/70 overflow-hidden">
        <div className="h-full bg-accent transition-[width] duration-500 ease-linear" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 flex items-center gap-3 text-[10px] text-zinc-500">
        <span>{fmtSize(p.bytes ?? 0)} / {fmtSize(p.size ?? 0)}</span>
        <span title="Instantaneous rate over the last poll — not the lifetime average">{fmtRate(p.speed)}</span>
        <span title="Remaining bytes at the current rate">ETA {fmtDur(p.eta_s ?? null)}</span>
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

/** The live view of a job started from a peer's share (Browse → auto-import).
 *
 *  The Auto tab's own way in is the queue — paste a release, `Add to queue` —
 *  and that path never asks a question (see ReleaseQueueBar). This card exists
 *  for the one remaining interactive start, where a human is looking at a
 *  peer's folder and said yes: its prompts still have somewhere to be answered,
 *  and the same pipeline runs behind them. */
function AutoJob() {
  const { t } = useI18n();
  const { data: job, refetch } = useQuery({
    queryKey: ["soulseekAuto"],
    queryFn: api.soulseekAutoStatus,
    // "confirm" is an active state too — the job is parked waiting for the
    // lossy-only go-ahead, so the card must appear promptly.
    refetchInterval: (q) => (q.state.data?.state === "running" || q.state.data?.state === "confirm" ? 2000 : 15000),
  });
  const running = job?.state === "running" || job?.state === "confirm";
  const navigate = useNavigate();
  const [answering, setAnswering] = useState(false);

  /** Run the job's own release again — no form to read the value from: the job
   *  that just ended carries the release it was about. */
  const startAgain = async () => {
    const id = job?.release?.id || "";
    if (!id) {
      toast("That job carried no release id — start it from the release's page");
      return;
    }
    try {
      const r = await api.soulseekAutoStart({ release_mbid: id });
      // Over the ceiling it does not fail — it takes its place and starts by
      // itself when a running release finishes (see the queue's Waiting group).
      toast(r.waiting
        ? `Queued — waiting for a free slot (position ${r.position})`
        : "Auto-import started");
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

  /** Answer the confirm prompt. Every variant shares this endpoint: for a lossy
   *  downgrade accept downloads the lossy copy, for an album without rip logs
   *  accept downloads it unverified, for an empty search accept parks the
   *  release in the wish list (the background worker keeps looking, so declining
   *  is the only way to actually stop). */
  const answer = async (accept: boolean) => {
    setAnswering(true);
    const reason = job?.confirm?.reason;
    try {
      await api.soulseekAutoConfirm(accept);
      toast(
        reason === "no_results"
          ? (accept ? "Moving it to the queue — the search keeps looking" : "Stopping — nothing was downloaded")
          : reason === "no_logs"
            ? (accept ? "Downloading without rip logs — it imports unverified" : "Stopped — waiting for a CD rip with logs")
            : (accept ? "Downloading the lossy copy" : "Stopped — waiting for a lossless copy")
      );
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
        toast.success("On the queue — the search keeps looking for it");
        return;
      }
      const album = fileName(job?.result?.album_path ?? job?.result?.staging_path ?? "");
      // The job reaches "done" the moment the album is IN the library, and the
      // import chain (links, metadata, cover art, the configured scripts) runs
      // on a thread of its own AFTER that — so "done" alone must not read as
      // "finished". `chain.running` is the server's own field for it.
      const chaining = !!job?.chain?.running;
      toast(album
        ? (chaining ? `Imported ${album} — ${t("queue.chain_running_brief")}` : `Imported ${album}`)
        : "Auto-import finished");
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
  // It is the REAL seconds this job's searches took (`soulseek_auto`'s
  // `_search_seconds`), never the configured ceiling — and 0 when a search
  // ended on its first poll, or when the job browsed a folder and never
  // searched at all. The old wording rendered nothing for 0, which read as if
  // the search had taken no time; sub-second now says so, and a prompt with no
  // query behind it (the browsed-folder path) says that instead of a duration
  // for a search that never ran.
  const waited = job?.confirm?.waited ?? 0;
  const searched = (job?.confirm?.queries ?? []).length > 0;
  const waitedTxt = waited >= 90 ? `${Math.round(waited / 60)} min` : waited >= 1 ? `${Math.round(waited)}s` : "under a second";
  const r = job?.release;
  // The wizard needs the folder it should tag; staging_path is the fallback an
  // unorganized job leaves in the result.
  const tagPath = job?.result?.album_path ?? job?.result?.staging_path ?? "";

  return (
    <div className="panel">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
          <Zap className="h-3.5 w-3.5" /> Live job — started from a peer's share
        </div>
        {running && (
          <button className="btn-ghost !py-1 text-xs text-red-300 tap" onClick={cancel}>
            <Square className="h-3 w-3" /> Stop
          </button>
        )}
      </div>
      <div className="text-[11px] text-zinc-500 mb-2.5">
        A folder you handed over from Browse runs the same chain the queue runs — search,
        rip-log test, download, audit, import — and stops here for the answers a queue row
        decides by policy. Releases go on the queue instead: paste one above.
      </div>
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
                className="btn-ghost !py-1 text-xs mt-2 text-red-300 tap"
                disabled={!job?.release?.id}
                onClick={startAgain}
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
                className="btn-ghost !py-1 text-xs mt-2 tap"
                disabled={!job?.release?.id}
                onClick={startAgain}
                title="Run the same release again with the values in the form above"
              >
                <RotateCw className="h-3.5 w-3.5" /> Start again
              </button>
            </div>
          )}
          {job?.state === "confirm" && job?.confirm && (job.confirm.reason === "no_results" ? (
            // A search that came back empty: the release is not on the network
            // right now, so the useful answer is "keep looking". Accept leaves it
            // on the queue, where the same search runs on the worker's own
            // schedule with no further input. Decline is the only way to stop.
            <div className="mt-2 rounded-lg border border-amber-700/60 bg-amber-950/30 p-2.5">
              <div className="text-xs font-semibold text-amber-300 mb-1">
                Nothing usable found{searched ? ` in ${waitedTxt}` : " — this job never searched a query"}
              </div>
              <div className="text-[11px] text-zinc-400 mb-2">
                Every candidate this search turned up was rejected or incomplete. Leaving the
                release on the queue keeps the same search running in the background — nothing else
                to answer, and it is imported automatically once a verified copy shows up.
                Stopping instead abandons this release until you ask for it again.
              </div>
              {(job.confirm.queries ?? []).length > 0 && (
                <div className="space-y-1 mb-2">
                  {(job.confirm.queries ?? []).map((q, i) => (
                    <div key={`${q}-${i}`} className="text-[11px] text-zinc-500 truncate" title={q}>searched “{q}”</div>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <button className="btn-primary !py-1 text-xs tap" onClick={() => answer(true)} disabled={answering}>
                  <Star className="h-3.5 w-3.5" /> Keep looking
                </button>
                <button className="btn-ghost !py-1 text-xs tap" onClick={() => answer(false)} disabled={answering}>
                  No, stop
                </button>
              </div>
            </div>
          ) : job.confirm.reason === "no_logs" ? (
            // Lossless and complete, but no rip log to verify it: the album is
            // still worth having, it just grades as what it is (a digital
            // release, or a rip nobody documented).
            <div className="mt-2 rounded-lg border border-amber-700/60 bg-amber-950/30 p-2.5">
              <div className="text-xs font-semibold text-amber-300 mb-1">
                No CD rip with logs found
              </div>
              <div className="text-[11px] text-zinc-400 mb-2">
                The complete album is available as {job.confirm.media || "a digital release"} (no
                .log/.cue). Download it anyway? It imports as {job.confirm.media || "that media"}
                and is graded accordingly.
              </div>
              <div className="space-y-1 mb-2">
                {(job.confirm.candidates ?? []).map((c) => (
                  <div key={`${c.username}\u0000${c.dir}`} className="text-[11px] text-zinc-500 truncate" title={c.dir}>
                    {c.format || "?"} · {c.matched}/{c.expected} tracks · {fmtSize(c.size)} · {c.username} · …{c.dir.slice(-40)}
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button className="btn-primary !py-1 text-xs tap" onClick={() => answer(true)} disabled={answering}>
                  <ArrowDownToLine className="h-3.5 w-3.5" /> Download without logs
                </button>
                <button className="btn-ghost !py-1 text-xs tap" onClick={() => answer(false)} disabled={answering}>
                  No, wait for a CD rip
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
              <div className="flex flex-wrap items-center gap-2">
                <button className="btn-primary !py-1 text-xs tap" onClick={() => answer(true)} disabled={answering}>
                  <ArrowDownToLine className="h-3.5 w-3.5" /> Download lossy anyway
                </button>
                <button className="btn-ghost !py-1 text-xs tap" onClick={() => answer(false)} disabled={answering}>
                  No, wait for lossless
                </button>
              </div>
            </div>
          ))}
          <div className="mt-1.5 max-h-44 overflow-auto font-mono text-[10px] leading-relaxed text-zinc-500 space-y-0.5 break-words">
            {(job?.log ?? []).map((l, i) => (
              <div key={`${l.t}-${i}`} className={l.msg.startsWith("ERROR") ? "text-red-400" : l.msg.startsWith("  ✕") ? "text-red-300" : undefined}>
                <span className="text-zinc-700 mr-1.5">{l.t}</span>{l.msg}
              </div>
            ))}
          </div>
          {(job?.attempts ?? []).length > 0 && (
            <details className="mt-1.5 text-[11px] text-zinc-500">
              <summary className="cursor-pointer">{job?.attempts?.length} rejected candidate(s) — the next candidate was tried</summary>
              <div className="mt-1 space-y-0.5">
                {/* slskd's own words ("User notfire appears to be offline") are
                    the whole point of this list, and there is no attempt cap any
                    more — so the reason is never cut short: it wraps in the row,
                    the peer carries the tail of the path, and the hover title
                    has the full path plus the reason. */}
                {(job?.attempts ?? []).map((a, i) => (
                  <div key={`${a.username}-${i}`} className="flex items-baseline gap-1.5" title={`${a.username} — ${a.dir}\n${a.reason}`}>
                    <span className="shrink-0 text-zinc-600">{a.username || "?"}</span>
                    <span className="min-w-0 truncate text-zinc-700">…{String(a.dir).slice(-40)}</span>
                    <span className="min-w-0 break-words text-zinc-400">{a.reason}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
          {job?.state === "done" && (job.result?.wished ? (
            // A keep-looking handoff ends the job done but with no album on
            // disk — the import row would otherwise show an empty name and a
            // dead button.
            <div className="mt-2 text-[11px] text-amber-300">
              Left on the queue — the search keeps looking for it.
            </div>
          ) : (
            <div className="mt-2 flex items-center gap-2 flex-wrap">
              <button
                className="btn-primary !py-1 text-xs tap"
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
                {job.chain?.running ? ` — ${t("queue.chain_running_brief")}` : ""}
                {!job.result?.organized ? " (organize failed — run it from the album page)" : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** The Auto tab: the queue IS the auto-importer.
 *
 *  A release goes on by pasting its MusicBrainz ID or URL, and from there the
 *  app does the whole job by itself — search on the worker's interval, test the
 *  rip logs, download, audit, import into the library. Nothing on that path
 *  asks a question: the cases an interactive job parked on (only lossy copies,
 *  no rip log, nothing found yet) are decided by the search policy, and a
 *  release that cannot be found simply stays on the queue and is looked for
 *  again on the next pass — the interval, the backoff and the attempt ceiling
 *  are the user's own, in Settings → Soulseek.
 *
 *  So this tab shows what is being LOOKED FOR, plus the one job that can still
 *  prompt: a peer's folder handed over from Browse (`AutoJob`). Everything else
 *  the pipeline is doing — downloading, verifying, needs-you, finished — is the
 *  Queue tab, one press away. */
function AutoPanel({ initialMbid, running, onShowQueue }: {
  initialMbid?: string;
  running: boolean;
  onShowQueue?: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const { data, refetch } = useQuery({
    queryKey: QUEUE_KEY,
    queryFn: api.queue,
    refetchInterval: running ? 3000 : 10000,
  });
  const { cancel, retry, dismiss, doImport, clear } = useQueueActions(refetch, setBusyId);
  const sections = data?.sections;
  // The releases WAITING for a free slot, in the order they will start, and the
  // rest of the "queued" section (a wish waiting for the network is being
  // searched, which is a different thing from waiting its turn).
  const waitingRows = (sections?.queued ?? []).filter((r) => r.waiting);
  const lookingRows = (sections?.queued ?? []).filter((r) => !r.waiting);
  // Every section is read through `queueRows`: optional chaining guards
  // `sections`, never the KEY inside it, so `sections.background.length` threw
  // on a payload from a server that predates the background section (a stale
  // cache, an older scratch server) and took the whole page down with it. A
  // missing section is "no rows in that section", which is the truth.
  const elsewhere = SECTIONS_ELSEWHERE.reduce(
    (n, name) => n + queueRows(sections, name).length, 0);

  return (
    <div className="space-y-4">
      <div className="panel">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5 mb-2">
          <Zap className="h-3.5 w-3.5" /> Auto-import a release
        </div>
        <div className="text-[11px] text-zinc-500 mb-2.5">
          Paste a MusicBrainz release ID or URL and the app finds it by its own identifiable
          traits (catalog number for CDs, title + year for digital media — the queries are yours
          in Settings → Soulseek), tests the rip logs before committing, downloads, audits
          against the logs and imports it fully tagged. It keeps looking on its own schedule
          until it lands: only lossy copies or a release with no rip log is decided by the search
          policy rather than by this app guessing, and a release nothing can be found for stays
          here and is searched again.
        </div>
        <ReleaseQueueBar initialMbid={initialMbid} onAdded={refetch} />
      </div>

      <AutoJob />

      {!sections ? (
        <PageLoading />
      ) : (
        <>
          {waitingRows.length > 0 && (
            <QueueSection
              title="Waiting"
              hint={`queued behind the ${data?.running ?? 0} release(s) running now — each starts by itself when one of them finishes`}
              rows={waitingRows} tone="border-amber-800 text-amber-300"
              empty=""
              busyId={busyId}
              onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
              onClear={(item) => clear({ id: item.id }, item)}
            />
          )}
          <QueueSection
            title="Looking for"
            hint="searched on the worker's own schedule — imported the moment a verified copy appears"
            rows={lookingRows} tone="border-amber-800 text-amber-300"
            empty="nothing is being looked for — paste a release above"
            busyId={busyId}
            onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
            onClear={(item) => clear({ id: item.id }, item)}
          />
          {queueRows(sections, "background").length > 0 && (
            <QueueSection
              title="Background"
              hint="asked every ranked edition it may, none answered — still searched on the worker's own ticks"
              rows={queueRows(sections, "background")}
              tone="border-violet-800 text-violet-300"
              empty="" busyId={busyId}
              onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
              onClear={(item) => clear({ id: item.id }, item)}
            />
          )}
          {elsewhere > 0 && (
            <div className="text-[11px] text-zinc-500">
              {elsewhere} row(s) downloading, parked, in the background or finished —{" "}
              <button className="underline hover:text-zinc-300 tap" onClick={onShowQueue}>
                open the Queue tab
              </button>{" "}
              for those. Nothing here needs a press: every release above starts, verifies and
              imports on its own.
            </div>
          )}
        </>
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
      const r = await api.soulseekAutoStart({ username, target_dir: d.directory });
      const folder = d.directory.split(/[\\/]/).filter(Boolean).pop() ?? "";
      toast(r.waiting
        ? `${username} · ${folder} is queued — waiting for a free slot (position ${r.position})`
        : `Auto-importing from ${username} · ${folder}`);
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
            className="btn-ghost !py-1 text-xs shrink-0 tap"
            disabled={isFetching}
            onClick={() => refetch()}
            title="Re-read the share list from slskd"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} />
          </button>
          <button
            className="btn-ghost !py-1 text-xs shrink-0 tap"
            disabled={busy !== null || picked.size === 0}
            onClick={queuePicked}
            title="Queue every file in the ticked folders"
          >
            <ArrowDownToLine className="h-3.5 w-3.5" /> Queue selected{picked.size > 0 ? ` (${picked.size})` : ""}
          </button>
          <button
            className="btn-ghost !py-1 text-xs shrink-0 tap"
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
        className="input w-full !py-1.5 text-xs tap"
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
                      className="btn-ghost !py-1 text-xs shrink-0 tap"
                      disabled={busy !== null || d.files.length === 0}
                      onClick={() => queue(d)}
                      title="Queue every file in this folder"
                    >
                      <ArrowDownToLine className="h-3.5 w-3.5" /> <span className="hidden sm:inline">Download</span>
                    </button>
                    <button
                      className="btn-ghost !py-1 text-xs shrink-0 tap"
                      disabled={busy !== null}
                      onClick={() => auto(d)}
                      title="Search the release this folder holds and import it fully tagged"
                    >
                      <Zap className="h-3.5 w-3.5" /> <span className="hidden sm:inline">Auto-import</span>
                    </button>
                  </div>
                  {isOpen && (
                    <div className="border-t border-border/60 max-h-64 overflow-auto">
                      {d.files.map((f) => (
                        <div key={f.filename} className="flex items-center gap-3 px-3 py-1 border-t border-border/40 first:border-t-0 text-xs">
                          <span className="flex-1 min-w-0 truncate text-zinc-300" title={f.filename}>{fileName(f.filename)}</span>
                          <span className="text-zinc-500 w-16 text-right shrink-0">{fmtSize(f.size)}</span>
                          <button
                            className="btn-ghost !px-1.5 !py-0.5 shrink-0 tap"
                            disabled={busy !== null}
                            onClick={() => queueFile(f)}
                            title="Queue this file on its own"
                          >
                            <ArrowDownToLine className="h-3 w-3" />
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
            <button className="btn-secondary w-full py-2 text-xs tap" onClick={() => setLimit((n) => n + 200)}>
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
          className={`btn-ghost !py-1 text-xs shrink-0 tap ${previewOpen ? "!text-accent" : ""}`}
          onClick={() => setPreviewOpen(!previewOpen)}
          title={f.is_video ? "Watch (preview player)" : "Listen (preview player)"}
        >
          <Play className="h-3.5 w-3.5" /> <span className="hidden sm:inline">Preview</span>
        </button>
        <button
          className={`btn-ghost !py-1 text-xs shrink-0 tap ${tagOpen ? "!text-accent" : ""}`}
          onClick={() => setTagOpen(!tagOpen)}
          title="Check / edit the tags before import — video files are remuxed to MKV on save (stream copy, no quality loss)"
        >
          <Tag className="h-3.5 w-3.5" /> <span className="hidden sm:inline">Tag</span>
        </button>
        <button
          className={`btn-ghost !py-1 text-xs shrink-0 tap ${armDelete ? "!text-red-300 border border-red-800" : ""}`}
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
                  className="input !py-1 !px-2 text-xs mt-0.5 w-full tap"
                  value={form[t.key] ?? ""}
                  placeholder={t.placeholder}
                  onChange={(e) => set(t.key, e.target.value)}
                />
              </label>
            ))}
            <label className="text-[10px] text-zinc-500 block">
              Advisory
              <select
                className="input !py-1 !px-2 text-xs mt-0.5 w-full tap"
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
            <button className="btn-primary !py-1 text-xs tap" disabled={busy} onClick={save}>
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

/** One import at a time, process-wide on the server: every button that starts
 *  one (a single wish, a single album, or all of them) reads and shows the
 *  same run, so no panel needs a job tracker of its own. */
const IMPORT_RUN_KEY = ["soulseekImportRun"];
const READY_ALBUMS_KEY = ["soulseekReady"];

function useImportRun() {
  return useQuery({
    queryKey: IMPORT_RUN_KEY,
    queryFn: api.importAllStatus,
    // The server keeps the last run's status forever, so the timer only runs
    // while there is something to watch.
    refetchInterval: (q) => (q.state.data?.state === "running" ? 1500 : false),
  });
}

/** Progress of the running import and the per-album verdicts once it stops.
 *  Renders nothing until a run exists, so the panels that offer Import need
 *  no "nothing started yet" branch. */
function ImportRunCard({ run }: { run: ImportRunStatus | undefined }) {
  const qc = useQueryClient();
  const state = run?.state ?? "idle";
  const wasRunning = useRef(false);
  // The run rewrites the library and empties the download dir, so the tabs
  // that read either one are refreshed once it stops.
  useEffect(() => {
    if (state === "running") {
      wasRunning.current = true;
      return;
    }
    if (!wasRunning.current) return;
    wasRunning.current = false;
    for (const queryKey of [["library"], ["soulseekReview"], READY_ALBUMS_KEY]) {
      qc.invalidateQueries({ queryKey });
    }
  }, [state, qc]);

  if (!run || (state === "idle" && run.results.length === 0)) return null;
  const running = state === "running";
  const failed = run.results.filter((r) => !r.ok).length;
  const pct = run.total ? Math.round((100 * run.done) / run.total) : 0;
  const cancel = async () => {
    try {
      const r = await api.importAllCancel();
      qc.setQueryData(IMPORT_RUN_KEY, r.status);
      toast(r.ok ? "Stopping after the album being imported…" : "No import is running");
    } catch (e) {
      toast.error(String(e));
    }
  };

  return (
    <div className="rounded-md border border-border bg-panel/60 p-2.5 space-y-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-widest text-zinc-500">Import run</span>
        {running ? (
          <span className="text-sky-300">
            <Loader2 className="h-3 w-3 inline animate-spin mr-1" />
            importing {Math.min(run.done + 1, run.total)} of {run.total} — {fileName(run.current ?? "") || "starting…"}
          </span>
        ) : (
          <span className="text-zinc-400">
            {state === "cancelled" ? "Stopped after" : "Imported"} {run.done} of {run.total} album(s)
            {failed ? <span className="text-red-300/80"> · {failed} failed</span> : ""}
            {run.finished_at ? ` · ${timeAgo(run.finished_at)}` : ""}
          </span>
        )}
        {running && (
          <button
            className="btn-ghost !py-1 text-xs ml-auto tap"
            onClick={cancel}
            title="Stop after the album being imported — never mid-album, a half-imported album is worse than a slow one"
          >
            <Square className="h-3.5 w-3.5" /> Cancel
          </button>
        )}
      </div>
      {running && (
        <div className="h-1.5 rounded-sm bg-border/70 overflow-hidden">
          <div className="h-full bg-accent transition-[width] duration-500" style={{ width: `${pct}%` }} />
        </div>
      )}
      {run.results.length > 0 && (
        <div className="space-y-0.5 max-h-48 overflow-auto stagger">
          {run.results.map((r) => (
            <div key={r.path} className="flex items-center gap-2 px-1 py-0.5">
              {r.ok ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
              ) : (
                <AlertTriangle className="h-3.5 w-3.5 text-red-400 shrink-0" />
              )}
              <span className="flex-1 min-w-0 truncate text-zinc-300" title={r.path}>
                {fileName(r.album_root || r.path)}
              </span>
              {!r.ok && (
                <span className="text-red-300/80 truncate max-w-[45%]" title={r.error}>{r.error || "failed"}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** How each stage of the ONE queue is drawn. The labels are the shared
 *  vocabulary (lib/acquisition's STAGE_LABEL, i.e. server/soulseek_auto.py
 *  STAGES) — a wish from MusicBrainz and a folder grabbed from the search box
 *  read the same, because they ARE the same queue, and so does the Library's
 *  badge for the same album. */
const QUEUE_STAGE: Record<string, { label: string; cls: string; icon: typeof Star }> = {
  queued: { label: STAGE_LABEL.queued, cls: "bg-zinc-800/70 text-zinc-300 border-zinc-700", icon: CircleDashed },
  searching: { label: STAGE_LABEL.searching, cls: "bg-sky-900/40 text-sky-300 border-sky-800", icon: Search },
  // The one stage that is not a server.soulseek_auto.STAGES name: an "Add to
  // library" that came back before MusicBrainz did
  // (server/pending_albums.STAGE_RESOLVING). Nothing is being searched for yet
  // — the server is working out WHAT the release is — so it says so instead of
  // wearing the download queue's own word.
  searching_musicbrainz: { label: STAGE_LABEL.searching_musicbrainz, cls: "bg-sky-900/40 text-sky-300 border-sky-800", icon: Search },
  downloading: { label: STAGE_LABEL.downloading, cls: "bg-sky-900/40 text-sky-300 border-sky-800", icon: ArrowDownToLine },
  verifying: { label: STAGE_LABEL.verifying, cls: "bg-cyan-900/40 text-cyan-300 border-cyan-800", icon: FileCheck2 },
  importing: { label: STAGE_LABEL.importing, cls: "bg-sky-900/40 text-sky-300 border-sky-800", icon: FolderInput },
  completed: { label: STAGE_LABEL.completed, cls: "bg-emerald-900/40 text-emerald-300 border-emerald-800", icon: ArrowDown },
  failed: { label: STAGE_LABEL.failed, cls: "bg-red-950/60 text-red-300 border-red-900", icon: AlertTriangle },
  needs_attention: { label: STAGE_LABEL.needs_attention, cls: "bg-amber-900/40 text-amber-300 border-amber-800", icon: AlertTriangle },
  // The fallback walk at rest (spec R153): every ranked edition this release
  // may ask was asked and none answered, so it keeps its place and is searched
  // again on the worker's own ticks. Deliberately its own colour: not amber
  // (nothing needs the user) and not sky (nothing is running right now).
  background: { label: STAGE_LABEL.background, cls: "bg-violet-950/60 text-violet-300 border-violet-900", icon: Clock },
};

/** The sections the Auto tab summarises as "elsewhere". */
const SECTIONS_ELSEWHERE = ["in_progress", "background", "needs_attention",
                            "completed", "failed"] as const;

/** One section of the queue payload, or an empty list.
 *
 *  `sections?.[name] ?? []` and never `sections?.name.length`: optional
 *  chaining guards the OBJECT, not the key — a payload from a server that
 *  predates a section (`background` is the newest, an older scratch server or a
 *  stale cached response carries none) made `sections.background.length` throw
 *  and blanked the whole Soulseek page, sidebar and all. A key the payload does
 *  not carry means that section has no rows. */
function queueRows(
  sections: SlskQueuePayload["sections"] | undefined,
  name: keyof SlskQueuePayload["sections"],
): SlskQueueItem[] {
  return sections?.[name] ?? [];
}

function QueueProgress({ p, stage }: { p: NonNullable<SlskQueueItem["progress"]>; stage: string }) {
  const { t } = useI18n();
  const pct = typeof p.percent === "number" ? Math.max(0, Math.min(100, p.percent)) : null;
  return (
    <div className="mt-1 space-y-0.5">
      <div className="flex items-center gap-2 text-[10px] text-zinc-500 flex-wrap">
        {pct !== null && <span className="text-zinc-400 w-9 text-right shrink-0 tabular-nums">{fmtPercent(pct)}</span>}
        {/* WHICH QUERY is being asked (a job asks its own list one after
            another) and slskd's own state for it — the difference between a
            search that is working and one stuck on a query nothing answers. */}
        {p.query && <span className="truncate max-w-[45%]" title={`${p.query}${p.state ? ` — slskd: ${p.state}` : ""}`}>{p.query}</span>}
        {p.files_total ? <span>{p.files_done ?? 0}/{p.files_total} file(s)</span> : null}
        {/* Files the job's own wait has ACCEPTED on disk, when that is a
            different number from what slskd calls complete. */}
        {!!p.files_arrived && p.files_arrived !== p.files_done && p.files_total
          ? <span>{p.files_arrived}/{p.files_total} file(s)</span>
          : null}
        {p.total ? <span>{fmtSize(p.done ?? 0)} / {fmtSize(p.total)}</span> : null}
        {p.speed ? <span className="text-sky-400/80">{fmtRate(p.speed)}</span> : null}
        {typeof p.eta_s === "number" && p.eta_s > 0 ? <span>ETA {fmtDur(p.eta_s)}</span> : null}
        {/* Searching is a ceiling a good candidate ends early — the counts are
            what the network really answered, never a fake countdown. */}
        {stage === "searching" && p.text ? <span className="truncate">{p.text}</span> : null}
      </div>
      {/* WHICH PEER'S copy is arriving, and from which folder on it: the one
          thing that says whose transfer this is, straight off the job's own
          download snapshot. */}
      {p.peer && (
        <div className="text-[10px] text-zinc-500 truncate" title={`${p.peer} — ${p.peer_dir}`}>
          {t("queue.peer", { user: p.peer, dir: p.peer_dir || "" })}
          {p.phase ? ` — ${p.phase}` : ""}
        </div>
      )}
      {pct !== null && (
        <div className="h-1 rounded-full bg-zinc-800 overflow-hidden">
          <div className="h-full bg-sky-600" style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}

/** The exact release a row is about, as one compact chip line.
 *
 *  In the order that identifies a PRESSING: the catalogue number and the
 *  medium(s) it is on first — those two are what tell two pressings of the
 *  same album apart — then the country and date and how much it carries, then
 *  the edition's own disambiguation and the status MusicBrainz states for it
 *  (Official / Promotion / Bootleg). The tooltip carries the same facts as one
 *  line.
 *
 *  Only what the server resolved is drawn. An identity with nothing in it (a
 *  release nobody has looked up yet, or a MusicBrainz outage) renders nothing
 *  at all, exactly like a row that never had one — never a dash pretending to
 *  be a value. */
function ReleaseChips({ r }: { r?: SlskReleaseIdentity }) {
  if (!r) return null;
  const catalog = (r.catalog_number ?? "").trim();
  const media = (r.media ?? []).filter((m) => m && m.trim());
  // The release's WHOLE event set when the server resolved one (`countries`),
  // never MusicBrainz's first event alone: a release out in five countries is
  // not a release out in the first of them. A payload predating the field
  // (a job's own compact summary) states the singular `country` only.
  const countries = (r.countries?.length ? r.countries : [r.country]).filter(Boolean).join(", ");
  const when = [countries, r.date].filter(Boolean).join(" ");
  const status = (r.status ?? "").trim();
  const paragraph = [
    catalog,
    media.join(" + "),
    when,
    r.track_count ? `${r.track_count} track(s)` : "",
    r.disambiguation ? `(${r.disambiguation})` : "",
    status,
    r.label ?? "",
  ].filter(Boolean).join(" · ");
  if (!paragraph) return null;
  // The status is a FACT about this pressing, not a judgement: an official
  // edition reads calm, a promotional/bootleg/withdrawn one is worth noticing.
  const statusCls = /official/i.test(status)
    ? "bg-emerald-900/40 text-emerald-300 border-emerald-800"
    : /promotion|bootleg|pseudo|withdraw|expire|cancel/i.test(status)
      ? "bg-amber-900/40 text-amber-300 border-amber-800"
      : "bg-zinc-800 text-zinc-400 border-zinc-700";
  return (
    <div className="text-[10px] text-zinc-500 truncate mt-0.5" title={paragraph}>
      <span className="inline-flex items-center gap-1 flex-wrap align-middle">
        {catalog && (
          <span className="chip text-[9px] border border-border bg-raise text-zinc-300">{catalog}</span>
        )}
        {media.map((m) => (
          <span key={m} className="chip text-[9px] border border-zinc-700 bg-zinc-800 text-zinc-300">{m}</span>
        ))}
        {when && <span>{when}</span>}
        {r.track_count ? <span>{r.track_count} track(s)</span> : null}
        {r.disambiguation ? <span className="text-zinc-600">({r.disambiguation})</span> : null}
        {status && <span className={`chip text-[9px] border ${statusCls}`}>{status}</span>}
        {r.label ? <span className="text-zinc-600">{r.label}</span> : null}
      </span>
    </div>
  );
}

/** One row of the queue: where it came from, what it is doing, how far along,
 *  and the one action that makes sense for it right now. */
function QueueRow({ item, busy, selected, onSelect, onCancel, onRetry, onImport, onDismiss, onClear }: {
  item: SlskQueueItem;
  busy: boolean;
  /** Selection state, in select mode only (`onSelect` absent = no checkbox).
   *  Keyed by `item.id`, which is the STABLE id its row is named by — the list
   *  is re-read every second, so an index-keyed tick would move under the
   *  user's finger. */
  selected?: boolean;
  onSelect?: (on: boolean) => void;
  onCancel: () => void;
  onRetry: () => void;
  onImport: () => void;
  onDismiss: () => void;
  onClear: () => void;
}) {
  const navigate = useNavigate();
  const { t } = useI18n();
  const st = QUEUE_STAGE[item.stage] ?? QUEUE_STAGE.queued;
  const Icon = st.icon;
  const active = item.stage === "searching" || item.stage === "downloading"
    || item.stage === "verifying" || item.stage === "importing";
  // The clock this row measures its own wait against, read ONCE per render: the
  // countdown is the server's own deadline minus this client's clock, so it is
  // never two different answers inside one row. `waitAt` is whichever deadline
  // the server published — the failure's backoff (`retry_at`) or the next look
  // the worker will take on its own (`due_at`) — and 0 when it published
  // neither, which is the case for a row that is being worked on right now and
  // for a terminal one nobody will look at again.
  const nowS = Date.now() / 1000;
  const waitAt = Math.max(Number(item.retry_at || 0), Number(item.due_at || 0));
  const waitIn = waitAt > nowS ? waitAt - nowS : 0;
  const label = item.title || item.artist || "(unknown release)";
  // One Retry button, whichever registry owns the row: a wish is re-armed and
  // searched (terminal or not — a "search now" is always allowed), a job's
  // release goes back into the pipeline. Only a row the server says can be
  // retried draws it.
  const canRetry = item.kind === "job"
    ? !!item.retryable
    : item.kind === "wish" && item.wish_id != null && item.stage !== "completed";
  const missing = item.missing_labels ?? [];
  return (
    <div className={`rounded-lg border bg-card overflow-hidden ${selected ? "border-sky-700" : "border-border"}`}>
      <div className="flex flex-wrap items-center gap-3 p-2.5">
        {onSelect && (
          <input type="checkbox" className="h-3.5 w-3.5 shrink-0 accent-sky-500 cursor-pointer tap"
            checked={!!selected} onChange={(e) => onSelect(e.target.checked)}
            title="Tick to include this row in the actions below" />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`chip text-[9px] border ${st.cls}`}>
              <Icon className={`h-3 w-3 ${active ? "animate-pulse" : ""}`} /> {st.label}
            </span>
            {/* Not started yet: it is in line behind the releases that are, and
                it says where. The page groups these rows as Waiting, so the
                two ideas — "queued, looking" and "queued, waiting its turn" —
                never read the same. */}
            {item.waiting && (
              <span className="chip text-[9px] border border-amber-800 bg-amber-900/30 text-amber-300"
                title="Not started: soulseek_search_concurrency releases are already running. This one starts by itself the moment one of them finishes.">
                <Clock className="h-3 w-3" /> Waiting{item.position ? ` · #${item.position}` : ""}
              </span>
            )}
            <span className="chip text-[9px] border border-border bg-raise text-zinc-400" title={
              item.source_key === "musicbrainz"
                ? "Saved from MusicBrainz — this queue searches it for you"
                : item.source_key === "auto"
                  ? "The auto-importer asked to keep looking for this one"
                  : item.source_key === "import"
                    ? "An import that could not finish the album by itself"
                    : "Started from a Soulseek search or browse"
            }>{item.source}</span>
            {item.attempts ? <span className="text-[10px] text-zinc-600">{item.attempts} attempt(s)</span> : null}
            {/* A ranked-edition WALK (spec R150-R153): this ONE row is working
                through the release group's editions, best first — one search
                window each — because the pressing it started on had nothing
                usable. The label is the server's own sentence
                (`wishes.candidate_state`), so the row, the album page and the
                notification cannot disagree about where the search is; the
                tooltip says which edition is being asked and what has already
                come back empty. Absent when there is nothing to walk (one
                candidate, or none). */}
            {item.walk && (
              <span
                className="chip text-[9px] border border-violet-800 bg-violet-900/30 text-violet-300"
                title={
                  `Also searching this release group's other pressings, ${item.walk.label}, one at a time — `
                  + `each gets its own search window, and an edition sharing a catalog number with one already asked is skipped. `
                  + (item.walk.title ? `Asking: ${item.walk.title}. ` : "")
                  + (item.walk.tried.length
                    ? `Came back empty: ${item.walk.tried.map((t) => t.title || t.mbid).join(", ")}.`
                    : "")
                }
              >
                <Layers className="h-3 w-3" /> {item.walk.label}
              </span>
            )}
            {item.progress?.percent != null && item.stage === "downloading" && (
              <span className="text-[10px] text-zinc-500">{fmtPercent(item.progress.percent)}</span>
            )}
          </div>
          <div className="text-sm text-zinc-100 truncate mt-0.5" title={label}>{label}</div>
          <div className="text-[11px] text-zinc-500 truncate">
            {item.artist}{item.artist && item.title ? " · " : ""}{item.artist ? item.title : ""}
          </div>
          {/* Which release this row is for — the exact pressing, on every row
              whose release is known (a wish's own block, a job's, a queued
              bulk release's). Facts only, no verb: the same ones sit on
              queued, downloading and finished rows, and "fetching" would be a
              lie on two of those three. A row that knows nothing shows
              nothing. */}
          <ReleaseChips r={item.release} />
          {item.note && <div className="text-[10px] text-zinc-500 truncate" title={item.note}>{item.note}</div>}
          {/* WHAT IT IS DOING RIGHT NOW, in the job's own words (the last line
              its log wrote): on a row with no bytes to show — a search, a wait
              for the album folder, a verification — this is the difference
              between a step that is working and one that is stuck. Absent on
              rows with nothing live behind them. */}
          {item.stage_text && (
            <div className="text-[10px] text-zinc-400 truncate" title={item.stage_text}>
              {t("queue.step", { text: item.stage_text })}
            </div>
          )}
          {/* WHICH EDITION it is asking for, on a live walk (spec R150-R153):
              the chip beside the stage says the POSITION ("Release 1 of 5");
              this says which pressing that position is. */}
          {item.walk?.title && active && (
            <div className="text-[10px] text-violet-300/90 truncate">
              {t("queue.asking", { label: item.walk.label, title: item.walk.title })}
            </div>
          )}
          {/* UNTIL WHEN, as a countdown off the server's own clock: a row that
              is WAITING says so instead of looking stuck. `retry_at` is the
              failure's backoff (the WHY is the reason line below); `due_at` is
              the next search the worker will run on its own. */}
          {!item.stage_text && waitIn > 0 && (
            <div className="text-[10px] text-zinc-400">
              {item.retry_at && item.retry_at > nowS
                ? t("queue.wait_retry", { when: fmtWait(waitIn) })
                : t("queue.wait_next", { when: fmtWait(waitIn) })}
            </div>
          )}
          {/* The album IS in the library and its chain is NOT finished: the
              import pipeline (links, metadata, cover art, the configured
              scripts) runs on a thread of its own after the download settles,
              so "imported" on its own would read as "done". */}
          {item.chain_running && (
            <div className="text-[10px] text-sky-300/90">{t("queue.chain_running")}</div>
          )}
          {/* WHY nothing has landed, candidate by candidate: the peers already
              tried, with slskd's own refusal for each. The server bounds the
              list; `rejected_count` is how many there really were, so a long
              search does not read as a short one. */}
          {(item.rejected?.length ?? 0) > 0 && (
            <details className="mt-0.5">
              <summary className="text-[10px] text-zinc-500 cursor-pointer tap">
                {t("queue.rejected", { count: item.rejected_count ?? item.rejected!.length })}
              </summary>
              <div className="mt-0.5 space-y-0.5">
                {item.rejected!.map((a, i) => (
                  <div key={`${a.username}-${i}`}
                    className="flex items-baseline gap-1.5 text-[10px] text-zinc-500"
                    title={`${a.username} — ${a.dir}\n${a.reason}`}>
                    <span className="shrink-0 text-zinc-400">{a.username || "?"}</span>
                    <span className="min-w-0 break-words text-zinc-400">{a.reason}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
          {/* What the album still NEEDS (an import that could not supply a
              family): a warning on a row whose release is finished — the album
              is in the library and graded, so this rides the finished row
              rather than holding it in a waiting section. "Enter manually"
              beside it is the wizard at the step that decides the first of
              them; "Mark complete" is the dismiss. A `reason` of "stopped" is
              the one case that really is waiting: a review import that has not
              run its chain yet. */}
          {missing.length > 0 && (
            <div className="text-[10px] text-amber-300/90 truncate"
              title={item.needs?.detail
                ? `${item.needs.detail}${item.needs.reason === "stopped" ? " — the import is waiting for this" : " — the album is in the library; enter it by hand or mark it complete"}`
                : missing.join(", ")}>
              <AlertTriangle className="inline h-3 w-3 align-[-1px]" /> Needs data:{" "}
              {missing.join(", ")}
            </div>
          )}
          {/* Partial bytes a rejected candidate could not free: named here so
              the failure is actionable rather than a folder full of orphans. */}
          {(item.leftovers?.length ?? 0) > 0 && (
            <div className="text-[10px] text-amber-300/80 truncate"
              title={(item.leftovers ?? []).join("\n")}>
              {item.leftovers!.length} partial file(s) left in the download folder
            </div>
          )}
          {item.reason && <div className="text-[10px] text-red-400/80 truncate" title={item.reason}>{item.reason}</div>}
          {item.progress && <QueueProgress p={item.progress} stage={item.stage} />}
        </div>
        <div className="flex flex-wrap items-center gap-1 shrink-0 w-full justify-end sm:w-auto">
          {item.kind === "ready" && (
            <button className="btn-primary !py-1 text-xs tap" onClick={onImport} disabled={busy}
              title="Import this album all the way through (convert, tag, organize, then the chain)">
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ArrowDownToLine className="h-3.5 w-3.5" />} Import
            </button>
          )}
          {/* MANUAL COMPLETION for a stalled album: the wizard opens ON this
              album at the step that decides the first thing it is missing
              (?album=…&step=…&missing=…), which is the same link its
              notification carries. */}
          {item.action === "manual" && item.action_link && (
            <button className="btn-primary !py-1 text-xs tap" disabled={busy}
              onClick={() => navigate(item.action_link!)}
              title={`Enter what is missing for ${label} by hand`}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />} Enter manually
            </button>
          )}
          {item.action === "answer" && item.action_link && (
            <button className="btn-ghost !py-1 text-xs tap" disabled={busy}
              onClick={() => navigate(item.action_link!)}
              title="Answer the question this download is parked on">
              <MessageCircleQuestion className="h-3.5 w-3.5" /> Answer…
            </button>
          )}
          {/* "The album is fine as it is": the prompt goes away and a later
              import of the same album raises it again only if it is still
              missing the family. */}
          {item.dismissable && (
            <button className="btn-ghost !py-1 text-xs tap" onClick={onDismiss} disabled={busy}
              title="Mark this album complete — stop asking about it">
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCheck className="h-3.5 w-3.5" />} Mark complete
            </button>
          )}
          {canRetry && (
            <button className="btn-ghost !py-1 text-xs tap" onClick={onRetry} disabled={busy}
              title={item.retryable
                ? "Retry this terminal item — it is not retried automatically"
                : "Search Soulseek for it right now"}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
            </button>
          )}
          {item.album_path && (
            <a className="btn-ghost !py-1 text-xs tap"
              href={`/album/${encodeURIComponent(item.album_path)}`}
              title="Open the album">
              <PackageOpen className="h-3.5 w-3.5" />
            </a>
          )}
          {item.release_mbid && (
            <a className="btn-ghost !py-1 text-xs tap"
              href={`https://musicbrainz.org/release/${item.release_mbid}`}
              target="_blank" rel="noreferrer" title="Open on MusicBrainz">
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
          {item.cancelable && (
            <button className="btn-ghost !py-1 text-xs text-red-300 tap" onClick={onCancel} disabled={busy}
              title={item.stage === "queued" ? "Take it back off the queue" : "Cancel this item"}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5" />}
            </button>
          )}
          {/* Take a FINISHED row off the list — never a second Cancel: the
              server says which rows may be cleared (`clearable`), and the
              wording below says what clearing that row means, because it is
              not the same thing for a wish (the standing request goes, with the
              framework folder it created) as for a finished job (only the
              history row goes — the album is already in the library). */}
          {item.clearable && (
            <button className="btn-ghost !py-1 text-xs text-red-300 tap" onClick={onClear} disabled={busy}
              title={item.kind === "wish"
                ? (item.pending
                  ? "Remove this row and its empty album folder — nothing is searched for it again"
                  : "Remove this row from the queue — nothing is searched for it again")
                : "Take this finished row off the queue — nothing in your library is deleted"}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function QueueSection({ title, hint, rows, tone, empty, busyId, selected, onSelect, onCancel, onRetry, onImport, onDismiss, onClear, onClearSection }: {
  title: string;
  hint: string;
  rows: SlskQueueItem[];
  tone: string;
  empty: string;
  busyId: string | null;
  /** The ticked row ids (see QueuePanel's select mode): a row draws a checkbox
   *  only when `onSelect` is given, so select mode is one flag at the panel. */
  selected?: Set<string>;
  onSelect?: (id: string, on: boolean) => void;
  onCancel: (item: SlskQueueItem) => void;
  onRetry: (item: SlskQueueItem) => void;
  onImport: (item: SlskQueueItem) => void;
  onDismiss: (item: SlskQueueItem) => void;
  onClear: (item: SlskQueueItem) => void;
  /** Clear THIS section's finished rows (POST /api/queue/clear, scope = the
   *  section). Absent for a section nothing may be cleared from. */
  onClearSection?: () => void;
}) {
  // The rows the section can lose, counted off the rows it shows — never off a
  // separate tally, so the button can never claim more than the list holds.
  const finished = rows.filter((r) => r.clearable).length;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`chip text-[10px] border ${tone}`}>{title} · {rows.length}</span>
        <span className="text-[11px] text-zinc-500">{hint}</span>
        {finished > 0 && onClearSection && (
          <button className="btn-ghost !py-0.5 !px-2 text-[10px] text-red-300 tap ml-auto"
            onClick={onClearSection} disabled={busyId !== null}
            title={`Take the finished rows off this list — the ${finished} the server marked clearable here. Nothing in your library is touched and nothing still running, waiting or parked goes; those are cancelled on their own row.`}>
            <Trash2 className="h-3 w-3" /> Clear finished ({finished})
          </button>
        )}
      </div>
      {rows.length === 0
        ? <div className="text-[11px] text-zinc-600 px-1">{empty}</div>
        : rows.map((item) => (
            <QueueRow
              key={item.id}
              item={item}
              busy={busyId === item.id}
              selected={selected?.has(item.id)}
              onSelect={onSelect ? (on) => onSelect(item.id, on) : undefined}
              onCancel={() => onCancel(item)}
              onRetry={() => onRetry(item)}
              onImport={() => onImport(item)}
              onDismiss={() => onDismiss(item)}
              onClear={() => onClear(item)}
            />
          ))}
    </div>
  );
}

/**
 *  THE queue: queued/searching, in-progress, needs-attention, completed and
 *  failed, in one list, for everything that is getting itself into the
 *  library — a MusicBrainz release waiting for a copy, a "download everything
 *  by this artist" run, a folder grabbed off the Soulseek page, an import run.
 *  The rows come from `/api/queue` (server/api_queue.py), which reads the
 *  registries that own them, so this panel never invents a state the backend
 *  does not have.
 *
 *  This list IS the wanted list: a release with no copy on the network is a
 *  row here that says it is still looking (with its own Search and Retry), and
 *  the panel's own box adds one. There is no second list to check — the store
 *  underneath (server.wishes) is the durable request, and the queue is where
 *  it is read, waited on and acted on.
 */
/** The five things that can be done to ONE queue row.
 *
 *  Extracted because two surfaces list the same rows — the Queue tab, and the
 *  Auto tab's "looking for" list — and a retry or a cancel has to mean the
 *  same thing, hit the same endpoint and say the same words wherever it is
 *  pressed. The caller owns `busyId` (each panel renders its own spinner) and
 *  passes the refetch its own queue query exposes. */
function useQueueActions(refetch: () => void, setBusyId: (id: string | null) => void) {
  const qc = useQueryClient();

  const cancel = async (item: SlskQueueItem) => {
    setBusyId(item.id);
    try {
      // ONE call, whatever the row is: POST /api/queue/cancel dispatches to
      // whatever owns it, and for a wish it ends the release WHOLE on the
      // server — the job filling it stopped (any status, not just
      // "searching"), an acquisition still waiting for a free pipeline slot
      // dropped, the framework album folder the add created taken down, and
      // the wish deleted. This used to send a "framework album" wish to
      // POST /api/library/add/cancel instead, which removed the folder and the
      // wish but left the running download alone: it kept going, settled as a
      // failure, and drew a SECOND row in Failed that needed its own Clear.
      await api.queueCancel(item.id);
      toast(item.stage === "queued" ? "Removed from the queue" : "Cancelled");
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusyId(null);
    }
  };
  const retry = async (item: SlskQueueItem) => {
    setBusyId(item.id);
    try {
      // A TERMINAL row (nothing found, or a job that gave up) is re-armed
      // first: the server never retries those by itself, so this press is the
      // only way back (POST /api/queue/retry). A wish that is merely waiting
      // is just searched now.
      if (item.retryable) {
        await api.queueRetry(item.id);
        toast("Retrying…");
      } else {
        if (item.wish_id == null) return;
        const r = await api.wishSearch(item.wish_id);
        if (!r.ok) toast(r.error || "Already searching");
        else toast("Searching…");
      }
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusyId(null);
    }
  };
  const dismiss = async (item: SlskQueueItem) => {
    if (!item.album_path) return;
    setBusyId(item.id);
    try {
      // "The album is fine as it is": the prompt goes away, and the next
      // import of the same album raises it again only if the family is really
      // still missing (server/api_imports.py's dismiss).
      await api.dismissImportPrompt(item.album_path);
      toast(`${item.title} — marked complete`);
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusyId(null);
    }
  };
  const doImport = async (item: SlskQueueItem) => {
    const path = item.path || "";
    if (!path) return;
    setBusyId(item.id);
    try {
      const r = await api.soulseekImportOne(path);
      qc.setQueryData(IMPORT_RUN_KEY, r.status);
      toast(`Importing ${item.title}…`);
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusyId(null);
    }
  };
  /** Take a finished row off the list (POST /api/queue/clear). Nothing in the
   *  library changes and no download is deleted — the row was history. The
   *  server refuses anything still in the pipeline, and its message names the
   *  action that IS meant for it (cancel), so the toast says what it said. */
  const clear = async (body: { id?: string; scope?: SlskQueueScope }, item?: SlskQueueItem) => {
    setBusyId(item?.id ?? "*");
    try {
      const r = await api.queueClear(body);
      toast(r.cleared === 1 && item
        ? `${item.title} — off the queue`
        : r.cleared ? `${r.cleared} finished row(s) cleared` : "Nothing finished to clear");
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusyId(null);
    }
  };

  return { cancel, retry, dismiss, doImport, clear };
}

/** "Look for this" — the one question the queue exists to answer.
 *
 *  Paste a MusicBrainz release, release-group or artist ID or URL and the release
 *  (or the group's best edition, or the artist's discography) becomes a row the
 *  wishes worker searches on its own schedule, through the SAME pipeline an
 *  auto-import job runs (search → log test → download → audit → import). The
 *  search happens on the interval below, and `Search due now` runs the pass
 *  that is due immediately — a row still waiting out its retry backoff keeps
 *  that wait, because what is due is the store's answer, not this button's.
 *
 *  Nothing on this path asks a question: a lossy-only or log-less candidate is
 *  decided by the search policy, and a release nothing can be found for stays
 *  on the queue and is tried again — which is what "keep looking" meant when it
 *  still had to be answered by hand.
 *
 *  `initialMbid` prefills the field (the MusicBrainz pages link here with
 *  `?release=<id>`), and only ever fills an EMPTY field: a late route change
 *  must not overwrite what someone is typing. */
function ReleaseQueueBar({ initialMbid, onAdded, className }: {
  initialMbid?: string;
  /** Called after a release was added, so the list under the bar can refetch. */
  onAdded?: () => void;
  className?: string;
}) {
  // The store's own worker state and log (GET /api/wishes): WHEN the app looks
  // for a release on its own, and what the last pass did — the schedule behind
  // these rows, read here rather than in a panel of its own.
  const { data: store } = useQuery({ queryKey: ["wishes"], queryFn: api.wishes, refetchInterval: 15000 });
  const worker = store?.worker;
  const [wanted, setWanted] = useState(initialMbid ?? "");
  const [wantBusy, setWantBusy] = useState(false);
  useEffect(() => {
    if (initialMbid) setWanted((cur) => cur || initialMbid);
  }, [initialMbid]);

  /** Want a release the network does not have yet: the row this creates is the
   *  standing request, with no album folder behind it (the album appears when
   *  something is found). The interval and the backoff decide when it is
   *  searched — this only records what to look for. */
  const addWanted = async () => {
    const ref = mbRef(wanted);
    if (!ref) {
      toast("Paste a MusicBrainz release, release-group or artist ID or URL");
      return;
    }
    setWantBusy(true);
    try {
      // The SAME entry point the MusicBrainz pages' own "Add to library" uses,
      // so a pasted link and a click on the release page queue identically: a
      // release or a release-group walks the group's ranked editions (R150),
      // an artist queues its discography in the background, and a bare id is
      // resolved by the server (`kind: "auto"`). Each album it creates is the
      // framework folder the worker searches for on its own schedule.
      const r = await api.libraryAdd({ mbid: ref.mbid, kind: ref.kind, mode: "best" });
      setWanted("");
      if (r.background) {
        toast.success(r.note || "Preparing the discography — the albums appear as they are added");
      } else {
        const created = (r.albums || []).filter((a) => a.created).length;
        const have = (r.albums || []).filter((a) => a.already_in_library).length;
        const skipped = (r.skipped || []).length + (r.errors || []).length;
        toast.success(
          `${created} album${created === 1 ? "" : "s"} queued`
          + (have ? ` · ${have} already in the library` : "")
          + (skipped ? ` · ${skipped} skipped` : "")
          + " — searched for automatically"
        );
      }
      onAdded?.();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setWantBusy(false);
    }
  };

  /** Search everything that is DUE, now, instead of at the next pass. */
  const searchDue = async () => {
    setWantBusy(true);
    try {
      const r = await api.wishesSearchAll();
      toast(r.ok ? "Searching for everything due…" : r.error || "Already searching");
      onAdded?.();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setWantBusy(false);
    }
  };

  return (
    <div className={`rounded-md border border-border bg-panel/60 p-2.5 space-y-2 text-xs ${className ?? ""}`}>
      <div className="flex flex-wrap gap-2">
        <Link2 className="h-4 w-4 text-zinc-600 self-center shrink-0" />
        <input
          className="input flex-1 min-w-[220px] tap"
          placeholder="Paste a MusicBrainz release, release-group or artist ID or URL — the queue keeps looking for it"
          value={wanted}
          onChange={(e) => setWanted(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !wantBusy && addWanted()}
        />
        <button
          className="btn-primary tap"
          onClick={addWanted}
          disabled={wantBusy || !wanted.trim()}
          title="Put it on the queue — Soulseek is searched for it on the interval below, and it is imported the moment a verified copy appears"
        >
          <Plus className="h-4 w-4" /> Add to queue
        </button>
        <button
          className="btn-ghost tap"
          onClick={searchDue}
          disabled={wantBusy}
          title="Search Soulseek for every row that is due right now, rather than at the next pass. A row still waiting out its retry backoff keeps that wait."
        >
          <Search className="h-3.5 w-3.5" /> Search due now
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-[10px] text-zinc-600">
        <span className={worker?.enabled ? "text-emerald-400" : "text-amber-400"}>
          {worker?.enabled ? "looking for these on its own" : "automatic search off"}
        </span>
        <span>· every {worker?.interval_hours ?? 6}h</span>
        <span>· next {worker?.next_run ? timeAgo(worker.next_run).replace("ago", "from now") : "—"}</span>
        {worker?.running && worker.current && (
          <span className="text-sky-300">· searching {worker.current}</span>
        )}
        {worker && !worker.running && worker.last_result && <span>· last: {worker.last_result}</span>}
      </div>
    </div>
  );
}

function QueuePanel({ running }: { running: boolean }) {
  const qc = useQueryClient();
  // The queue follows the server's payload on this interval: fast while
  // something is moving (progress bars, stage changes, a prompt being answered
  // elsewhere), slow while the queue is only sitting there. Every mutation
  // below refetches at once, so an action never waits for the next tick.
  const { data, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: QUEUE_KEY,
    queryFn: api.queue,
    refetchInterval: running ? 3000 : 10000,
  });
  const [busyId, setBusyId] = useState<string | null>(null);
  // SELECT MODE: the ticks are keyed by the row's own id — the STABLE one the
  // server names it by ("job:3", "pipeline:<key>", …). The list is polled every
  // few seconds and rows start, settle and drop out of it as it goes, so an
  // index-keyed selection would move under the user's finger; an id that has
  // left the list simply stops counting (see `selectedRows`).
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const sections = data?.sections;
  // What the header says: the rows it is actually rendering, added up. The
  // server's own `counts` come off the same rows, so the two can never
  // disagree — and if a payload ever arrived without them, the list is still
  // the thing the numbers describe.
  const rows = Object.values(sections ?? {}).flat();
  const total = rows.length;

  // The bar (paste → Add to queue), the wishes worker behind it and the five
  // row actions are shared with the Auto tab: one hook and one component, so
  // the two surfaces cannot drift.
  const { cancel, retry, dismiss, doImport, clear } = useQueueActions(refetch, setBusyId);
  // The store's own worker log (GET /api/wishes): what the last passes did. The
  // bar polls the same key for its schedule line, so this shares that cache.
  const { data: store } = useQuery({
    queryKey: ["wishes"],
    queryFn: api.wishes,
    refetchInterval: 15000,
  });

  // Rows whose work is over — the count the header's "Clear finished" reports,
  // and the only rows that button will touch.
  const finished = rows.filter((r) => r.clearable).length;

  // The releases WAITING for a free slot, in the order they will start, and the
  // rest of the "queued" section (a wish waiting for the network is being
  // searched, which is a different thing from waiting its turn).
  const waitingRows = (sections?.queued ?? []).filter((r) => r.waiting);
  const queuedRows = (sections?.queued ?? []).filter((r) => !r.waiting);
  // The ticked rows STILL in the list: the only ones a bulk action may touch,
  // and the number its button reports. A tick whose row left the queue (it
  // started, settled or was cancelled elsewhere) stops counting here.
  const selectedRows = rows.filter((r) => selected.has(r.id));
  const commitCount = selectedRows.filter(
    (r) => (r.kind === "ready" && r.path) || r.clearable).length;

  const toggleSelected = (id: string, on: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  /** Cancel EXACTLY the ticked rows, by id (POST …/downloads/cancel): a release
   *  that has not started is dropped before it downloads a byte, a running one
   *  is stopped, and the server answers how many of the ids it acted on and
   *  which ones had already gone. */
  const cancelSelected = async () => {
    if (selectedRows.length === 0) return;
    setBulkBusy(true);
    const ids = selectedRows.map((r) => r.id);
    try {
      const r = await api.queueCancelIds(ids);
      toast(
        r.cancelled === 1 && ids.length === 1
          ? `${selectedRows[0].title || selectedRows[0].artist || "1 row"} — cancelled`
          : `${r.cancelled} of ${ids.length} selected row(s) cancelled`
            + (r.missed.length ? ` · ${r.missed.length} had already gone` : "")
      );
      setSelected(new Set());
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBulkBusy(false);
    }
  };

  /** The stage-appropriate COMMIT for the ticked rows: a finished download is
   *  IMPORTED (the only thing left to do with it), a settled row is taken off
   *  the list, and a row still running or waiting has no commit at all — it is
   *  skipped and counted as skipped rather than silently acted on. */
  const commitSelected = async () => {
    const ready = selectedRows.filter((r) => r.kind === "ready" && r.path);
    const settled = selectedRows.filter((r) => r.clearable);
    if (ready.length === 0 && settled.length === 0) return;
    setBulkBusy(true);
    let moved = 0, cleared = 0, failed = 0;
    try {
      for (const row of ready) {
        try {
          const r = await api.soulseekImportOne(row.path || "");
          qc.setQueryData(IMPORT_RUN_KEY, r.status);
          moved += 1;
        } catch {
          failed += 1;
        }
      }
      for (const row of settled) {
        try {
          await api.queueClear({ id: row.id });
          cleared += 1;
        } catch {
          failed += 1;
        }
      }
      const parts = [
        moved ? `${moved} import(s) started` : "",
        cleared ? `${cleared} row(s) off the list` : "",
        failed ? `${failed} refused` : "",
      ].filter(Boolean);
      toast(parts.join(" · ") || "Nothing on the selected rows to commit");
      setSelected(new Set());
      refetch();
    } finally {
      setBulkBusy(false);
    }
  };

  /** Clear all: every QUEUED/WAITING release goes in one press, before any of
   *  them starts. A running release is untouched — that is a cancel, on its own
   *  row — and the server says how many waiting rows it removed. */
  const clearWaiting = async () => {
    setBulkBusy(true);
    try {
      const r = await api.queueClearWaiting();
      toast(r.cleared
        ? `${r.cleared} waiting release(s) taken off the queue`
        : "Nothing was waiting to clear");
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-border bg-panel/60 p-2.5 flex flex-wrap items-center gap-2 text-xs">
        <span className="font-semibold text-zinc-300 flex items-center gap-1.5">
          <ArrowDownUp className="h-3.5 w-3.5" /> Pipeline
        </span>
        <span className="text-[11px] text-zinc-500">
          {sections ? `${total} item(s)` : "…"}
          {/* The three numbers the pipeline runs on, all shipped and all
              editable in Settings → Soulseek: releases at once (the overflow
              WAITS), candidates per release, and slskd's transfer slots — the
              product of the other two, which the app never relies on to hold
              either limit. */}
          {data ? ` · ${data.running}/${data.concurrency} running` : ""}
          {data?.candidate_slots ? ` · ${data.candidate_slots} candidate(s) each` : ""}
          {data?.download_slots ? ` · ${data.download_slots} slskd transfer slot(s)` : ""}
          {/* When these numbers were fetched — the poll is not a live feed, and
              saying so is the difference between "0 items" and "0 items a
              while ago". The Refresh button beside it refetches now. */}
          {dataUpdatedAt ? ` · fetched ${new Date(dataUpdatedAt).toLocaleTimeString()}` : ""}
        </span>
        <span className="text-[10px] text-zinc-600 hidden sm:inline">
          the same queue for MusicBrainz releases, bulk auto-imports and manual grabs
        </span>
        <div className="ml-auto flex items-center gap-1 flex-wrap justify-end">
          {waitingRows.length > 0 && (
            <ConfirmButton
              className="btn-ghost !py-0.5 !px-2 text-[11px] text-red-300 tap"
              onConfirm={clearWaiting} disabled={bulkBusy}
              confirmLabel={`Clear ${waitingRows.length} waiting?`}
              title={`Empty the WAITING queue — the ${waitingRows.length} release(s) queued behind the ones running now go in one press, before any of them starts. A release that is RUNNING is not touched (cancel it on its own row), and nothing in your library, no settled row and no slskd transfer is affected.`}>
              <Trash2 className="h-3 w-3" /> Clear all ({waitingRows.length})
            </ConfirmButton>
          )}
          <button className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
            onClick={() => { setSelectMode((v) => !v); setSelected(new Set()); }}
            disabled={bulkBusy}
            title="Tick rows to act on several at once — cancel them, or import/commit them. The ticks are keyed by each row's own id, so they stay on the rows you picked while the list refreshes.">
            <CheckSquare className="h-3 w-3" /> {selectMode ? "Done selecting" : "Select"}
          </button>
          {finished > 0 && (
            <button className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
              onClick={() => clear({ scope: "finished" })} disabled={busyId !== null}
              title="Take every finished row off this list — imported albums, jobs that gave up, rows nothing was found for. Nothing in your library is touched, no download is deleted, and anything still running, waiting or parked stays (clear it per section, or cancel it on its own row).">
              <Trash2 className="h-3 w-3" /> Clear finished ({finished})
            </button>
          )}
          <button className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
            onClick={() => refetch()} disabled={isFetching}
            title="Reload the queue from the server now — the list also follows it on its own: every 3 s while something is moving, every 10 s when nothing is.">
            <RefreshCw className={`h-3 w-3 ${isFetching ? "animate-spin" : ""}`} /> Refresh
          </button>
        </div>
      </div>

      {/* Select mode's own bar: what is ticked, and the two things that can be
          done to it. It acts on exactly the ticked rows (the ids the server
          names them by) and says how many it affected. */}
      {selectMode && (
        <div className="rounded-md border border-sky-800 bg-sky-950/30 p-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-[11px] text-zinc-300">
            {selectedRows.length} selected
            {selected.size > selectedRows.length
              ? ` · ${selected.size - selectedRows.length} left the queue` : ""}
          </span>
          <span className="text-[10px] text-zinc-500">
            tick rows below — cancel acts on every ticked row, import/commit on
            the finished ones
          </span>
          <button className="btn-ghost !py-0.5 !px-2 text-[11px] text-red-300 tap"
            onClick={cancelSelected} disabled={bulkBusy || selectedRows.length === 0}
            title="Cancel every TICKED row in one call, by their own ids: a release still waiting never starts, a running one stops at its next step. Nothing unticked is touched.">
            {bulkBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <X className="h-3 w-3" />}
            {" "}Cancel selected ({selectedRows.length})
          </button>
          <button className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
            onClick={commitSelected} disabled={bulkBusy || commitCount === 0}
            title="Do the thing each ticked row is FOR: a finished download in the folder is imported into the library, and a settled row is taken off the list. Anything else is skipped — a running or waiting release has nothing to commit yet, and a row parked on a question is answered on its own row.">
            <CheckCircle2 className="h-3 w-3" /> Import / commit selected ({commitCount})
          </button>
        </div>
      )}

      {/* The same bar the Auto tab renders: one component, two places —
          a release added on either tab is added the same way. */}
      <ReleaseQueueBar onAdded={refetch} />

      {!sections ? (
        <PageLoading />
      ) : (
        <>
          {/* The waiting queue first: these releases have NOT started — they are
              in line behind the releases at the pipeline's ceiling, in the order
              they will run. Each one starts by itself when a slot frees, so this
              group is read-and-cancel, not a list of things to press. */}
          {waitingRows.length > 0 && (
            <QueueSection
              title="Waiting"
              hint={`queued behind the ${data?.running ?? 0} release(s) running now — each starts by itself when one of them finishes`}
              rows={waitingRows} tone="border-amber-800 text-amber-300"
              empty=""
              busyId={busyId}
              selected={selectMode ? selected : undefined}
              onSelect={selectMode ? toggleSelected : undefined}
              onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
              onClear={(item) => clear({ id: item.id }, item)}
            />
          )}
          <QueueSection
            title="Queued / searching" hint="looking right now, or waiting for the network"
            rows={queuedRows} tone="border-amber-800 text-amber-300"
            empty="nothing is waiting — paste a release above, or start an auto-import"
            busyId={busyId}
            selected={selectMode ? selected : undefined}
            onSelect={selectMode ? toggleSelected : undefined}
            onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
            onClear={(item) => clear({ id: item.id }, item)}
          />
          <QueueSection
            title="In progress" hint="downloading, verifying, moving into the library, or running the import chain"
            rows={queueRows(sections, "in_progress")} tone="border-sky-800 text-sky-300"
            empty="nothing is downloading right now"
            busyId={busyId}
            selected={selectMode ? selected : undefined}
            onSelect={selectMode ? toggleSelected : undefined}
            onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
            onClear={(item) => clear({ id: item.id }, item)}
          />
          {queueRows(sections, "background").length > 0 && (
            <QueueSection
              title="Background"
              hint="every ranked edition this release may ask was asked and none answered — it keeps its place and is searched again on the worker's own ticks, until one lands"
              rows={queueRows(sections, "background")}
              tone="border-violet-800 text-violet-300"
              empty="" busyId={busyId}
              selected={selectMode ? selected : undefined}
              onSelect={selectMode ? toggleSelected : undefined}
              onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
              onClear={(item) => clear({ id: item.id }, item)}
            />
          )}
          {queueRows(sections, "needs_attention").length > 0 && (
            <QueueSection
              title="Needs you" hint="parked on a question, or waiting for a manual import"
              rows={queueRows(sections, "needs_attention")} tone="border-amber-800 text-amber-300"
              empty="" busyId={busyId}
              selected={selectMode ? selected : undefined}
              onSelect={selectMode ? toggleSelected : undefined}
              onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
              onClear={(item) => clear({ id: item.id }, item)}
              onClearSection={() => clear({ scope: "needs_attention" })}
            />
          )}
          <QueueSection
            title="Completed" hint="downloaded — and whether it made it into the library"
            rows={queueRows(sections, "completed")} tone="border-emerald-800 text-emerald-300"
            empty="nothing has finished yet"
            busyId={busyId}
            selected={selectMode ? selected : undefined}
            onSelect={selectMode ? toggleSelected : undefined}
            onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
            onClear={(item) => clear({ id: item.id }, item)}
            onClearSection={() => clear({ scope: "completed" })}
          />
          <QueueSection
            title="Failed" hint="gave up, with the reason"
            rows={queueRows(sections, "failed")} tone="border-red-900 text-red-300"
            empty="nothing failed"
            busyId={busyId}
            selected={selectMode ? selected : undefined}
            onSelect={selectMode ? toggleSelected : undefined}
            onCancel={cancel} onRetry={retry} onImport={doImport} onDismiss={dismiss}
            onClear={(item) => clear({ id: item.id }, item)}
            onClearSection={() => clear({ scope: "failed" })}
          />
        </>
      )}

      {store?.log && store.log.length > 0 && (
        <details className="panel">
          <summary className="text-[10px] uppercase tracking-widest text-zinc-500 cursor-pointer">
            Search log
          </summary>
          <div className="mt-2 space-y-0.5 max-h-48 overflow-auto font-mono text-[10px]">
            {store.log.slice(-40).reverse().map((l) => (
              // The window slides as the worker appends: an index key remounts
              // every line on each poll, the line's own stamp + text does not.
              <div key={`${l.t}-${l.msg}`}
                className={l.level === "warn" ? "text-amber-400/80" : l.level === "ok" ? "text-emerald-400/80" : "text-zinc-500"}>
                {new Date(l.t * 1000).toLocaleTimeString()} — {l.msg}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

/** The albums that finished downloading and are waiting to be imported — the
 *  same list a full import run would take. Each row sends ONE album through
 *  the whole pipeline on its own, unlike the one-click Import completed
 *  button above, which moves everything at once. */
function ReadyImports() {
  const qc = useQueryClient();
  const { data, isFetching, refetch } = useQuery({
    queryKey: READY_ALBUMS_KEY,
    queryFn: api.soulseekReady,
    // Sizing every album walks the download dir server-side, so this is
    // fetched on mount and after an action, never on a timer.
    staleTime: 30000,
    refetchOnWindowFocus: false,
  });
  const { data: run } = useImportRun();
  const runBusy = run?.state === "running";
  const [busyPath, setBusyPath] = useState<string | null>(null);
  const albums = data?.albums ?? [];

  const start = async (a: ReadyAlbum) => {
    setBusyPath(a.path);
    try {
      const r = await api.soulseekImportOne(a.path);
      qc.setQueryData(IMPORT_RUN_KEY, r.status);
      toast(`Importing ${a.name}…`);
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusyPath(null);
    }
  };

  return (
    <div className="rounded-md border border-border bg-panel/60 p-2.5 space-y-2 mb-2.5">
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="font-semibold text-zinc-300 flex items-center gap-1.5">
          <PackageOpen className="h-3.5 w-3.5" /> Finished downloads
        </span>
        <span className="text-[11px] text-zinc-500">
          {albums.length
            ? `${albums.length} album(s) waiting in ${data?.download_dir ?? "the download folder"} — each Import takes one album through on its own`
            : "nothing finished is waiting to be imported"}
        </span>
        <button
          className="btn-ghost !py-0.5 !px-2 text-[11px] ml-auto tap"
          onClick={() => refetch()}
          disabled={isFetching}
          title="Rescan the download folder"
        >
          <RefreshCw className={`h-3 w-3 ${isFetching ? "animate-spin" : ""}`} /> Rescan
        </button>
      </div>
      {albums.map((a) => (
        <div key={a.path} className="flex items-center gap-2 px-2 py-1 rounded hover:bg-white/[0.04] text-xs">
          <div className="flex-1 min-w-0">
            <div className="truncate text-zinc-200" title={a.path}>{a.name}</div>
            <div className="text-[10px] text-zinc-600 truncate" title={a.rel}>{a.rel}</div>
          </div>
          <span className="text-zinc-500 w-8 text-right shrink-0" title={`${a.files} file(s)`}>{a.files} f</span>
          <span className="text-zinc-500 w-16 text-right shrink-0">{fmtSize(a.bytes)}</span>
          <button
            className="btn-primary !py-0.5 !px-2 text-[11px] shrink-0 tap"
            disabled={runBusy || busyPath !== null}
            onClick={() => start(a)}
            title="Import this album all the way through — convert, tag, organize, then the import chain"
          >
            {busyPath === a.path
              ? <Loader2 className="h-3 w-3 animate-spin" />
              : <ArrowDownToLine className="h-3 w-3" />} Import
          </button>
        </div>
      ))}
      <ImportRunCard run={run} />
    </div>
  );
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
          <button className="btn-ghost !py-1 text-xs tap" onClick={() => refetch()} title="Rescan the download folder">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
          <button className="btn-primary !py-1 text-xs tap" onClick={importAll} title="Ingest completed downloads into the library">
            <ArrowDownToLine className="h-3.5 w-3.5" /> Import completed
          </button>
        </div>
      </div>
      <div className="text-[11px] text-zinc-500 mb-2.5">
        Preview each download, fix its tags (untagged DVD/Blu-ray rips included — saving remuxes them to MKV without
        re-encoding), discard the misses, then import the keepers. Files stay in{" "}
        <span className="font-mono text-zinc-400">{data?.dir ?? "…"}</span> until an import finishes — nothing is moved
        out of it before that.
      </div>
      <ReadyImports />
      {albums.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2.5">
          {albums.map(([dir, count]) => (
            <button
              key={dir}
              className="btn-ghost !py-1 text-xs tap"
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
              className="btn-ghost !py-1 text-xs tap"
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

/** What other Soulseek users can see of this library, derived server-side from
 *  slskd's own state (scan state, live share list, live filters) and compared
 *  with the disk. One `status` per distinct failure mode, so "nobody can see my
 *  files" arrives with its cause instead of a guess. `browse` is only filled in
 *  by the on-demand probe (it reads slskd's whole share index). */
interface SlskShareAudit {
  ok: boolean;
  status: string;
  summary: string;
  problems: { code: string; message: string; hint: string }[];
  notes: string[];
  shares: {
    configured: { path: string; alias: string }[];
    live: { host: string; local: string; remote: string; alias: string;
            files: number; directories: number; excluded: boolean }[];
    mismatch: boolean;
    dropped: { path: string; reason: string }[];
  };
  filters: { applied: string[];
             invalid: { pattern: string; reason: string }[];
             mismatch: boolean };
  scan: { state: string; scanning: boolean; pending: boolean; ready: boolean;
          faulted: boolean; cancelled: boolean; progress: number; files: number;
          directories: number; log: string[] };
  disk: { roots: { path: string; exists: boolean; readable: boolean;
                   audio_files: number }[];
          audio_files: number; truncated: boolean; probe_file: string };
  browse: { checked: boolean; ok: boolean | null; directories: number;
            detail: string };
  port: { listen_port: number; container: boolean };
  running: boolean;
}

/** Chip tone per audit status: green only when slskd really is serving the
 *  index, amber for "working but incomplete", red for a share nobody sees. */
const AUDIT_TONE: Record<string, string> = {
  ok: "bg-emerald-900/40 text-emerald-300 border-emerald-800",
  scanning: "bg-sky-900/40 text-sky-300 border-sky-800",
  misconfigured: "bg-amber-900/40 text-amber-300 border-amber-800",
  disabled: "bg-raise border-border text-zinc-400",
};

/** Share configuration (la musica settings are the source of truth — the
 * slskd yaml is regenerated from them at start) with the live share audit,
 * rescan and the autostart preference. Reserved folders (.mlo/data /
 * .mlo/downloads / .mlo/trash) are filtered server-side and never shared. */
function SharingCard({ running }: { running: boolean }) {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["soulseekShares"], queryFn: () => api.soulseekShares() });
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [newDir, setNewDir] = useState("");
  const [busy, setBusy] = useState(false);
  // The probed audit replaces the polled one until the next fetch: it is the
  // only one that looked inside the index, so it must not be overwritten by a
  // cheaper answer.
  const [probed, setProbed] = useState<SlskShareAudit | null>(null);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    if (data && dirs === null) setDirs((data.dirs as string[]) ?? []);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const autostart: boolean = data?.autostart ?? true;
  const audit = (probed ?? (data?.audit as SlskShareAudit | undefined)) ?? null;
  const savedDirs: string[] = data?.dirs ?? [];
  const dirty =
    dirs !== null &&
    (JSON.stringify([...dirs].sort()) !== JSON.stringify([...savedDirs].sort()));

  const refresh = () => {
    setProbed(null);
    qc.invalidateQueries({ queryKey: ["soulseekShares"] });
  };

  const save = async (autostartOverride?: boolean) => {
    setBusy(true);
    try {
      const r = await api.soulseekSharesSave(dirs ?? [], autostartOverride ?? null, true);
      toast(r.restarted ? "Shares saved — slskd restarted and rescanning" : "Shares saved");
      refresh();
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
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  /** Read slskd's own share index and look for a file that is on disk — the
   *  browse a remote user would get. Reported as observed, including "could
   *  not tell". */
  const verifyBrowsable = async () => {
    setProbing(true);
    try {
      const r = await api.soulseekShares(true);
      const a = r.audit as SlskShareAudit;
      setProbed(a);
      const b = a.browse;
      if (b.ok === true) toast.success(`Browse verified — ${b.detail}`);
      else if (b.ok === false) toast.error(`Browse check failed — ${b.detail}`);
      else toast(`Browse check could not confirm anything: ${b.detail || "no answer"}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setProbing(false);
    }
  };

  const applyConfig = async () => {
    setBusy(true);
    try {
      await api.soulseekRestart();
      toast("slskd restarted — reading the share config");
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleAutostart = async () => {
    setBusy(true);
    try {
      // `dirs` is null until the shares query answers: saving then would post
      // an empty list, and the server stores it verbatim (every configured
      // share folder would be gone). The checkbox is disabled in that window.
      await api.soulseekSharesSave(dirs ?? [], !autostart, false);
      refresh();
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
        {audit && (
          <span className={`chip text-[9px] border ${AUDIT_TONE[audit.status] ?? "bg-red-900/40 text-red-300 border-red-800"}`}>
            {audit.status === "ok" ? "shared" : audit.status.replace(/_/g, " ")}
          </span>
        )}
        {audit?.scan.ready && audit.scan.files > 0 && (
          <span className="chip text-[9px] border bg-raise border-border text-zinc-400">
            {fmtCount(audit.scan.files)} files · {fmtCount(audit.scan.directories)} folders indexed
          </span>
        )}
        <div className="ml-auto flex items-center gap-2.5">
          <label className="flex items-center gap-1.5 cursor-pointer text-zinc-400" title="Start slskd automatically when the app starts">
            <input
              type="checkbox"
              checked={autostart}
              onChange={() => toggleAutostart()}
              disabled={busy || dirs === null}
              title={dirs === null ? "Waiting for the share list to load" : undefined}
            />
            Start with the app
          </label>
          <button
            className="btn-ghost !py-1 text-xs tap"
            onClick={verifyBrowsable}
            disabled={probing || !running}
            title="Read slskd's own share index and look for a file that is on disk — what a browse by another user returns"
          >
            {probing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Eye className="h-3.5 w-3.5" />} Verify browse
          </button>
          <button className="btn-ghost !py-1 text-xs tap" onClick={rescan} disabled={busy || !running}>
            <RefreshCw className="h-3.5 w-3.5" /> Rescan
          </button>
        </div>
      </div>

      {audit && (
        <div className="space-y-1.5">
          <div className={`${audit.ok ? "text-zinc-400" : audit.status === "scanning" || audit.status === "misconfigured" ? "text-amber-300" : "text-red-300"}`}>
            {audit.summary}
          </div>
          {audit.problems.length > 0 && (
            <ul className="space-y-1">
              {audit.problems.map((p) => (
                <li key={p.code + p.message} className="rounded border border-border/60 bg-raise/40 px-2 py-1.5">
                  <div className="text-[11px] text-zinc-300">{p.message}</div>
                  {p.hint && <div className="text-[10px] text-zinc-500 mt-0.5">{p.hint}</div>}
                </li>
              ))}
            </ul>
          )}
          {audit.shares.mismatch && running && (
            <button className="btn-ghost !py-1 text-xs tap text-amber-300" onClick={applyConfig} disabled={busy}>
              <RotateCw className="h-3.5 w-3.5" /> Restart slskd to apply the generated config
            </button>
          )}
          {audit.browse.checked && (
            <div className={`text-[10px] ${audit.browse.ok === true ? "text-emerald-300" : audit.browse.ok === false ? "text-red-300" : "text-zinc-500"}`}>
              browse check: {audit.browse.detail || "no answer"}
            </div>
          )}
          {audit.scan.log.length > 0 && (
            <div className="text-[10px] text-zinc-600 font-mono truncate" title={audit.scan.log.join("\n")}>
              slskd scan log: {audit.scan.log[audit.scan.log.length - 1]}
            </div>
          )}
          {audit.notes.map((n) => (
            <div key={n} className="text-[10px] text-zinc-600">{n}</div>
          ))}
        </div>
      )}
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
              className="input !py-1 flex-1 font-mono text-[11px] tap"
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
              className="btn-ghost !py-1 text-xs tap"
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
            <button className="btn-primary !py-1 text-xs w-full tap" onClick={() => save()} disabled={busy}>
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

/** Hard stop for the search poll loop, in seconds. slskd ends a search 15s
 *  after the last peer response by default, which lands well inside this. */
const SEARCH_POLL_LIMIT_S = 180;

/* ------------------------------------------------------------------------- *
 * Frames into caches
 *
 * The transfer watcher (server/main.py) pushes the rows /api/soulseek/downloads
 * returns, the minute they change, on the shell's progress socket. These three
 * merges put a frame into the cache each surface already draws from, so no
 * component learns about a second transport and no number is computed twice:
 * every value below is the server's own, either the route's row or the job's
 * published progress block. Each returns null when the frame restated what
 * the cache held, which is what keeps a 2.5 Hz channel from re-rendering a
 * panel that has nothing new to show.
 * ------------------------------------------------------------------------- */

/** This job's block from the newest frame (null when the frame does not
 *  mention it — it belongs to another job, or to none). */
function liveJob(frame: TransfersFrame | null, id: number | null | undefined) {
  if (!frame || id === null || id === undefined) return null;
  return (frame.jobs ?? []).find((j) => j.id === id) ?? null;
}

/** The Downloads tab's rows. */
function mergeDownloads(old: SlskDownloads | undefined, frame: TransfersFrame): SlskDownloads | null {
  if (!old) return null;
  const byId = new Map((frame.files ?? []).map((r) => [r.id, r]));
  if (byId.size === 0) return null;
  let changed = false;
  const downloads = (old.downloads ?? []).map((user) => ({
    ...user,
    directories: (user.directories ?? []).map((d) => ({
      ...d,
      files: (d.files ?? []).map((f) => {
        const live = byId.get(f.id);
        if (!live) return f;
        const next = {
          ...f,
          bytesTransferred: live.bytesTransferred ?? f.bytesTransferred,
          size: live.size ?? f.size,
          percentComplete: live.percentComplete ?? f.percentComplete,
          state: live.state ?? f.state,
          averageSpeed: live.averageSpeed ?? f.averageSpeed,
        };
        if (next.bytesTransferred === f.bytesTransferred && next.size === f.size
            && next.percentComplete === f.percentComplete && next.state === f.state
            && next.averageSpeed === f.averageSpeed) return f;
        changed = true;
        return next;
      }),
    })),
  }));
  return changed ? { downloads } : null;
}

/** The queue rows' progress blocks. The route builds that block out of these
 *  very fields (server/api_queue.py's `_progress_from_download`), so a patched
 *  row shows the job's own numbers — the same ones its Auto-import card draws
 *  — instead of a second measurement of them. */
function mergeQueue(old: SlskQueuePayload | undefined, frame: TransfersFrame): SlskQueuePayload | null {
  if (!old) return null;
  let changed = false;
  const patch = (row: SlskQueueItem) => {
    const p = liveJob(frame, row.job_id)?.progress;
    if (!p || !row.progress) return row;
    const next = {
      ...row.progress,
      percent: p.percent ?? row.progress.percent,
      files_done: p.files_done ?? row.progress.files_done,
      files_total: p.files_total ?? row.progress.files_total,
      speed: p.speed ?? row.progress.speed,
      eta_s: p.eta_s ?? row.progress.eta_s,
      done: p.bytes ?? row.progress.done,
      total: p.size ?? row.progress.total,
    };
    const same = next.percent === row.progress.percent
      && next.files_done === row.progress.files_done
      && next.files_total === row.progress.files_total
      && next.speed === row.progress.speed
      && next.eta_s === row.progress.eta_s
      && next.done === row.progress.done
      && next.total === row.progress.total;
    if (same) return row;
    changed = true;
    return { ...row, progress: next };
  };
  // Structurally this is the same five-key object Object.entries walks; the
  // cast is only because fromEntries cannot know the keys.
  const sections = Object.fromEntries(
    Object.entries(old.sections).map(([name, rows]) => [name, rows.map(patch)]),
  ) as SlskQueuePayload["sections"];
  return changed ? { ...old, sections } : null;
}

function timeAgo(t: number | null | undefined): string {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default function SoulseekPage() {
  const [params] = useSearchParams();
  const qc = useQueryClient();
  const live = useLiveTransfers();
  // Every frame the transfer watcher pushes, into the cache the surfaces
  // below already draw from. One effect for the whole page: the frame is one
  // message about one slskd tree, and each merge decides for itself whether
  // its surface has anything new (they return null when it does not).
  useEffect(() => {
    if (!live) return;
    // The list changed shape — a transfer appeared or slskd dropped one —
    // which a patch cannot express: that is one real refetch.
    if (live.resync) qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    qc.setQueryData<SlskDownloads>(["soulseekDownloads"], (old) => mergeDownloads(old, live) ?? old);
    qc.setQueryData<SlskQueuePayload>(QUEUE_KEY, (old) => mergeQueue(old, live) ?? old);
    // The Auto-import card is the job's whole payload, so a state or stage
    // flip (searching → downloading → importing) means its log, release and
    // result changed too: refetch once for that, and patch only the numbers.
    const job = qc.getQueryData<SlskAutoJob>(["soulseekAuto"]);
    const liveAuto = liveJob(live, job?.id);
    if (job && liveAuto && (liveAuto.state !== job.state || liveAuto.stage !== job.stage)) {
      qc.invalidateQueries({ queryKey: ["soulseekAuto"] });
    } else if (job && liveAuto?.progress) {
      qc.setQueryData<SlskAutoJob>(["soulseekAuto"], { ...job, progress: liveAuto.progress });
    }
  }, [live, qc]);
  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ["soulseekStatus"],
    queryFn: api.soulseekStatus,
    refetchInterval: 10000,
  });
  const { data: downloads, refetch: refetchDownloads } = useQuery({
    queryKey: ["soulseekDownloads"],
    queryFn: api.soulseekDownloads,
    enabled: !!status?.running,
    // A SAFETY NET, not the bar's cadence: the transfers themselves arrive
    // pushed (see the effect below), and this is what keeps the list right if
    // that socket is down — a fallback, so it can be slow. Measured before the
    // push existed: with 3 s the bar moved every 3.02 s.
    refetchInterval: 30000,
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
  // Whoever slskd is really signed in as — the saved `username` drifts from it
  // the moment the login is corrected on slskd's own page.
  const account = String(status?.account ?? "").trim();

  // Start/stop transitions poll status once a second (instead of waiting for
  // the 10s background refresh) so pressing the button feels immediate.
  const [pending, setPending] = useState<null | "start" | "stop">(null);
  useEffect(() => {
    if (!pending) return;
    // 3s — the same cadence as this page's fastest list poll, and deliberately
    // NOT removed in favour of the websocket's own invalidation (another
    // component owns that frame, and a start/stop spinner that never clears if
    // it stops arriving is worse than a slower one). A 1s refetch here
    // re-rendered every panel on the page once a second.
    const iv = setInterval(refetchStatus, 3000);
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

  // The port check (server/api_soulseek.py) is asked for ON DEMAND and never on
  // its own: it probes the router and tries a connection to the public address,
  // which a page load has no business doing. `enabled: false` is what keeps
  // react-query from fetching it before the button does, the answer then stays in
  // the cache until the user hides it, and a second press probes again. The button
  // is never disabled for a running transfer — the probe is read-only and takes no
  // lock, which is the point of asking it while downloads are in flight.
  const { data: portCheck, isFetching: portCheckBusy, refetch: probePort } = useQuery({
    queryKey: ["soulseekPortCheck"],
    queryFn: api.soulseekPortCheck,
    enabled: false,
  });
  const testPort = async () => {
    try {
      const r = await probePort();
      if (r.error) toast.error(String(r.error));
    } catch (e) {
      toast.error(String(e));
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
  const releaseParam = params.get("release") ?? undefined;

  // Page tabs — Queue is the default: it is the one place that shows
  // EVERYTHING on its way into the library (wishes, bulk auto-imports, manual
  // grabs, imports). The badge on Downloads counts active transfers so
  // progress is visible from any tab.
  type TabId = "queue" | "search" | "auto" | "downloads" | "messages" | "sharing" | "settings";
  const TAB_LIST: { id: TabId; label: string }[] = [
    { id: "queue", label: "Queue" },
    { id: "search", label: "Search" },
    { id: "auto", label: "Auto-import" },
    { id: "downloads", label: "Downloads" },
    { id: "messages", label: "Messages" },
    { id: "sharing", label: "Sharing" },
    { id: "settings", label: "Settings" },
  ];
  // The cached tracks used to be a tab here. That list is the Downloads page's
  // now, so a link that still names the old tab lands on it instead of falling
  // back to the queue: the reader asked for their downloads either way.
  const wantCached = params.get("tab") === "cached";
  const navigate = useNavigate();
  useEffect(() => {
    if (wantCached) navigate("/downloads", { replace: true });
  }, [wantCached, navigate]);
  const [tab, setTab] = useState<TabId>(() => {
    // A link can name the tab it wants: a queue row's "Answer…" opens the tab
    // the parked question is answered on (`/soulseek?tab=auto`), and the
    // notifications link back to the queue.
    const want = params.get("tab") ?? "";
    if (TAB_LIST.some((t) => t.id === want)) return want as TabId;
    return releaseParam ? "auto" : "queue";
  });
  const { data: queue } = useQuery({
    queryKey: QUEUE_KEY,
    queryFn: api.queue,
    enabled: running,
    refetchInterval: running ? 5000 : false,
  });
  const queueBusy = (queue?.counts.queued ?? 0) + (queue?.counts.in_progress ?? 0)
    + (queue?.counts.needs_attention ?? 0);
  const dlFiles = (downloads?.downloads ?? []).flatMap((u: any) =>
    (u.directories ?? []).flatMap((d: any) =>
      (d.files ?? []).map((f: any) => ({ ...f, username: u.username, dir: d.directory }))));
  const dlActive = dlFiles.filter((f: any) => f.state === "InProgress" || f.state === "Queued").length;
  // The shared Segmented renders plain labels, so the live counts ride in the
  // label string; every tab still reads as the same control as the rest of the
  // app's switchers.
  const TAB_OPTIONS = TAB_LIST.map((t) => ({
    id: t.id,
    label:
      t.id === "downloads" && dlActive > 0 ? `${t.label} · ${dlActive}`
      : t.id === "messages" && msgUnread > 0 ? `${t.label} · ${msgUnread}`
      : t.id === "queue" && queueBusy > 0 ? `${t.label} · ${queueBusy}`
      : t.id === "search" && results.length > 0 ? `${t.label} · ${results.length}`
      : t.label,
  }));

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
          // that were still collecting. Every backend terminal state counts
          // (backend is_search_done); anything else is still collecting.
          const isDone =
            Boolean(res.isComplete) ||
            (res.state ? /Completed|TimedOut|ResponseLimitReached|Cancelled|Errored|FileLimitReached/.test(res.state) : false);
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
              <span className="text-emerald-400">
                running
                {status?.logged_in
                  ? ` · ${account ? `signed in as ${account}` : "logged in"}`
                  : status?.logged_in === false ? " · not logged in" : ""}
              </span>
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
              <button className="btn-ghost tap" onClick={stop} disabled={pending !== null}>
                {pending === "stop" ? "Stopping…" : <><Power className="h-4 w-4" /> Stop</>}
              </button>
            ) : (
              <button className="btn-primary tap" onClick={start} disabled={pending !== null}>
                {pending === "start" ? "Starting…" : <><Play className="h-4 w-4" /> Start slskd</>}
              </button>
            )}
            <button className="btn-ghost tap" onClick={() => { refetchStatus(); refetchDownloads(); }} title="Reload status and the transfer list">
              <RefreshCw className="h-4 w-4" />
            </button>
          </>
        }
      >
        {/* Tabs live in the header's extra row — they belong to the title. */}
        <Segmented className="max-w-full flex-wrap" value={tab} onChange={setTab} options={TAB_OPTIONS} />
      </PageHeader>

      {tab === "settings" && status && (
        <div className="panel flex items-center gap-2 flex-wrap text-xs">
          <span className="text-[10px] uppercase tracking-widest text-zinc-500 mr-1">Ports</span>
          <label className="flex items-center gap-1.5 text-zinc-500">
            Listen (Soulseek)
            <input
              className="input w-28 !py-1 !px-2 font-mono tap"
              inputMode="numeric"
              value={listenPort}
              onChange={(e) => setListenPort(e.target.value.replace(/\D/g, ""))}
              title="Port the Soulseek network sees (listen_port in slskd)"
            />
          </label>
          <label className="flex items-center gap-1.5 text-zinc-500">
            Web UI
            <input
              className="input w-28 !py-1 !px-2 font-mono tap"
              inputMode="numeric"
              value={webPort}
              onChange={(e) => setWebPort(e.target.value.replace(/\D/g, ""))}
              title="Local slskd API/web port"
            />
          </label>
          <button className="btn-ghost !py-1 text-xs tap" disabled={!portsChanged || portsBusy} onClick={savePorts}>
            <Save className="h-3.5 w-3.5" /> {portsBusy ? "Saving…" : "Save"}
          </button>
          <span className="text-[10px] text-zinc-600">
            saved in settings · {running ? "saving restarts slskd to apply" : "applied at the next start"}
          </span>
          {/* What the LISTEN port is really doing, on its own line: whether
              anything accepts on it here and what the router was told. */}
          <span className="w-full flex flex-wrap items-center gap-2 text-[10px] text-zinc-600">
            <span className="uppercase tracking-widest">Listen port</span>
            <ListenPortState state={status.listen_port_state} />
            <button
              className="btn-ghost !py-1 !px-2 text-[10px] tap ml-auto"
              onClick={testPort}
              disabled={portCheckBusy}
              title="Check the port from this machine: a listener here, what the router holds for it, the addresses, and a connection to the public address. Read-only, so it works while transfers run — a definite answer about the internet needs a probe from outside, which this app does not ship."
            >
              {portCheckBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
              {portCheckBusy ? "Testing…" : "Test port"}
            </button>
          </span>
        </div>
      )}

      {tab === "settings" && portCheck && (
        <PortCheckPanel
          result={portCheck}
          onHide={() => qc.removeQueries({ queryKey: ["soulseekPortCheck"] })}
        />
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

      {status?.listen_port_state?.conflict && (
        <ListenPortConflict
          message={String(status.listen_port_state.conflict)}
          port={Number(status.listen_port_state.listen_port) || 0}
        />
      )}

      {!status?.conflict && running && status?.logged_in === false && (
        status?.has_credentials ? (
          <ReconnectingCard
            username={String(status.username ?? "")}
            password={String(status.password ?? "")}
            error={(status?.error as string | null) ?? null}
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

      {tab === "auto" && (
        <AutoPanel initialMbid={releaseParam} running={running} onShowQueue={() => setTab("queue")} />
      )}

      {tab === "queue" && <QueuePanel running={running} />}

      {tab === "search" && (
      <div className="panel-hero">
        <div className="flex flex-wrap gap-2">
          <input
            className="input flex-1 tap"
            placeholder="Search Soulseek manually (artist — album, title, catalog #…)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !searching && runSearch()}
          />
          <button className="btn-primary tap" onClick={() => runSearch()} disabled={searching || !running}>
            <Search className="h-4 w-4" /> {searching ? "Searching…" : "Search"}
          </button>
          {searching && (
            <button className="btn-ghost tap" onClick={cancelSearch} title="Stop this search — slskd drops it and the results stop polling">
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
                className="chip px-2 py-0.5 border bg-raise border-border text-zinc-400 hover:text-white max-w-[280px]"
                disabled={searching || !running}
                onClick={() => runSearch(q)}
                title={q}
              >
                {/* The chip is inline-flex, so the ellipsis belongs on this
                    child — a `truncate` on the chip clips both ends. */}
                <span className="truncate min-w-0">{q}</span>
              </button>
            ))}
            <button
              className="btn-ghost !px-1.5 !py-0 text-[11px] text-zinc-500 tap"
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
                    className="input !py-1 !w-auto text-[11px] tap"
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
                <button className="btn-ghost !py-0.5 !px-2 text-[11px] tap" onClick={expandAll}>Expand all</button>
                <span>·</span>
                <button className="btn-ghost !py-0.5 !px-2 text-[11px] tap" onClick={collapseAll}>Collapse all</button>
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
                        className="flex-1 min-w-0 w-full sm:w-auto text-left"
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
                          className="btn-ghost !py-1 text-xs shrink-0 tap"
                          disabled={logTest[g.key] === "busy"}
                          onClick={() => testLogs(g)}
                          title="Download only the .log file(s) and grade them before committing to the album"
                        >
                          <FileCheck2 className="h-3.5 w-3.5" /> Test logs
                        </button>
                      )}
                      <button
                        className="btn-ghost !py-1 text-xs shrink-0 tap"
                        onClick={() => setBrowseUser(g.username)}
                        title={`Browse everything ${g.username} shares`}
                      >
                        <FolderOpen className="h-3.5 w-3.5" /> Browse
                      </button>
                      <button
                        className="btn-ghost !py-1 text-xs shrink-0 tap"
                        disabled={busyUser === g.username || !g.files.length}
                        onClick={() => downloadFile(g.files[0], true)}
                        title="Download this whole folder"
                      >
                        <ArrowDownToLine className="h-3.5 w-3.5" /> Folder
                      </button>
                    </div>
                    {open && (
                      <div className="border-t border-border/60">
                        {g.files.map((f) => (
                          <div key={f.file} className="flex items-center gap-3 px-3 py-1.5 border-t border-border/40 first:border-t-0 text-xs">
                            <span className="flex-1 min-w-0 truncate text-zinc-300" title={fileName(f.file)}>
                              {fileName(f.file)}
                            </span>
                            <span className="text-zinc-500 w-14 sm:w-16 text-right shrink-0">{fmtSize(f.size)}</span>
                            {/* bitrate is a desktop column: the size and the
                                extension already identify the file on a phone */}
                            <span className="hidden sm:block text-zinc-500 w-20 text-right shrink-0">
                              {f.bitrate ? `${f.bitrate}${f.vbr ? " vbr" : ""}` : extOf(f.file) || "—"}
                            </span>
                            <span className="text-zinc-500 w-10 text-right shrink-0">{fmtDur(f.duration)}</span>
                            <button
                              className="btn-ghost !px-1.5 !py-0.5 shrink-0 tap"
                              disabled={busyUser === g.username}
                              onClick={() => downloadFile(f, false)}
                              title="Download this file"
                            >
                              <ArrowDownToLine className="h-3 w-3" />
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
                  className="btn-secondary w-full py-2 text-xs mt-2 tap"
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
            <button className="btn-ghost !py-0.5 !px-2 text-[11px] tap" onClick={cancelSearch} title="Stop this search">
              Cancel search
            </button>
          </div>
        )}
      </div>
      )}

      {tab === "downloads" && (
        <>
          <ReviewPanel />
          <StagingImportPanel />
          <DownloadsPanel downloads={downloads} status={status} />
          <StagingPanel />
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
  // Not unconditionally, though: the thread poll replaces the list every few
  // seconds, and a scrollTop write on every poll yanks the reader (and on iOS
  // the page) back to the bottom while they are reading history. So a thread
  // that was just opened jumps to the newest message, and an open one only
  // follows when the reader is already at the bottom.
  const scrollRef = useRef<HTMLDivElement>(null);
  const openedFor = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (openedFor.current !== open) {
      openedFor.current = open;
      el.scrollTop = el.scrollHeight;
      return;
    }
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight;
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
          className="input !py-1 !px-2 text-xs w-full sm:w-56 tap"
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
          className="btn-ghost !py-1 text-xs tap"
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
        <div className="flex flex-col sm:flex-row gap-3 mt-3">
          <div className="w-full sm:w-56 shrink-0 rounded-lg border border-border bg-panel/60 p-1 space-y-0.5 max-h-[420px] overflow-auto stagger">
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
                    className="btn-ghost !px-1.5 !py-0.5 text-[10px] tap"
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
                    className="btn-primary !py-1 text-xs self-end tap"
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
 * Retry re-queues a failed one, and the Clear buttons sweep the history per
 * scope — the incomplete one also deletes the partial bytes it stops. */
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
  // The share slskd reports, kept fractional: the bar and the chip are the
  // same number, and a big file has to be able to move between two whole
  // percents.
  const pct = (f: SlskTransfer) => {
    if (typeof f.percentComplete === "number") return Math.max(0, Math.min(100, f.percentComplete));
    if (f.size) return Math.max(0, Math.min(100, (100 * (f.bytesTransferred ?? 0)) / f.size));
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
      <span className="chip text-[9px] bg-sky-900/40 text-sky-300 border border-sky-800">{fmtPercent(pct(f))}</span>
    ) : has(f, "Queued") ? (
      <span className="chip text-[9px] bg-raise border border-border text-zinc-400">queued</span>
    ) : (
      <span className="chip text-[9px] bg-red-950/60 text-red-300 border border-red-900">{f.state.toLowerCase() || "Failed"}</span>
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

  /** One handler for every bulk clear. The scope picks what slskd drops, and
   *  `incomplete` also deletes the partial bytes already on disk — that one is
   *  confirmed by name first, because those bytes cannot be resumed. */
  const clear = async (scope: "finished" | "failed" | "incomplete" | "all", what?: string) => {
    if (what && !window.confirm(`Delete ${what}?\n\nThe partial bytes already on disk go with them and cannot be resumed.`)) return;
    setBusy("*");
    try {
      const r = await api.soulseekDownloadsClear(scope);
      const freed = r.bytes_freed ? ` · ${fmtSize(r.bytes_freed)} freed` : "";
      const refused = r.failed?.length ? ` · ${r.failed.length} refused` : "";
      toast(r.cleared ? `${r.cleared} cleared${freed}${refused}` : `Nothing to clear${refused}`);
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
              className="btn-ghost !py-0.5 !px-2 text-[11px] ml-1 tap"
              disabled={busy !== null}
              onClick={() => clear("finished")}
              title="Remove finished / failed transfers from this list (in-progress and queued transfers are kept)"
            >
              <Trash2 className="h-3 w-3" /> Clear finished
            </button>
          )}
          {active.length + queued.length > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
              disabled={busy !== null}
              onClick={() => clear(
                "incomplete",
                `${active.length + queued.length} in-flight transfer(s) and the partial bytes already downloaded`,
              )}
              title="Stop what is still in flight and delete the partial bytes it left on disk — as destructive as it sounds, and it asks first"
            >
              <Trash2 className="h-3 w-3" /> Clear incomplete ({active.length + queued.length})
            </button>
          )}
          {failed.length > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
              disabled={busy !== null}
              onClick={() => clear("failed")}
              title="Remove only the failed transfers from this list (completed ones stay, nothing on disk is touched)"
            >
              <Trash2 className="h-3 w-3" /> Clear failed
            </button>
          )}
          {active.length + queued.length > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
              disabled={busy !== null}
              onClick={() => cancel([...active, ...queued])}
              title="Drop every running and queued transfer from slskd's queue"
            >
              <Square className="h-3 w-3" /> Cancel all active
            </button>
          )}
          {failed.length > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
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
          <Segmented
            className="mb-2"
            value={view}
            onChange={setView}
            options={[
              { id: "active", label: `Active (${active.length + queued.length})` },
              { id: "completed", label: `History (${completed.length + failed.length})` },
            ]}
          />
          <div className="space-y-1 max-h-[360px] overflow-auto stagger">
            {shown.map((f) => (
              <div key={f.id || `${f.username}\u0000${f.filename}`} className="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-white/[0.04] text-xs">
                <div className="flex-1 min-w-0">
                  <div className="truncate text-zinc-200" title={f.filename}>{fileName(f.filename ?? "")}</div>
                  <div className="text-[10px] text-zinc-600 truncate" title={f.dir}>{f.username} · {f.dir}</div>
                </div>
                {view === "active" && (
                  <div className="hidden sm:block w-16 sm:w-28 shrink-0 h-1.5 rounded-sm bg-border/70 overflow-hidden">
                    <div className={`h-full ${f.state === "InProgress" ? "bg-accent" : "bg-zinc-600"}`} style={{ width: `${pct(f)}%` }} />
                  </div>
                )}
                <span className="text-zinc-500 w-14 sm:w-16 text-right shrink-0">{fmtSize(f.size ?? 0)}</span>
                {/* slskd reports the running average per transfer — hidden on
                    phones, where the row has no width to spare */}
                <span className="text-zinc-500 w-20 text-right shrink-0 hidden sm:block" title="Average transfer rate">
                  {fmtRate(f.averageSpeed)}
                </span>
                <span className="w-14 sm:w-16 text-right shrink-0">{bucket(f)}</span>
                {view === "active" ? (
                  <button
                    className="btn-ghost !px-1.5 !py-0.5 text-[11px] shrink-0 w-16 justify-center tap"
                    disabled={busy !== null}
                    onClick={() => cancel([f])}
                    title="Drop this transfer from slskd's queue"
                  >
                    <Square className="h-3 w-3" /> Cancel
                  </button>
                ) : failed.includes(f) ? (
                  <button
                    className="btn-ghost !px-1.5 !py-0.5 text-[11px] shrink-0 w-16 justify-center tap"
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

/** Rendered rows per staging root. The totals above the list always describe
 *  EVERYTHING on disk, so a long root is capped with a "+N more" line rather
 *  than painting thousands of rows. */
const STAGING_ROWS = 50;

const stagingTotals = (count: number, bytes: number) =>
  `${count} ${count === 1 ? "entry" : "entries"} · ${fmtSize(bytes)}`;

/** One staging root. The two are independent — either may not exist yet, and a
 *  name only means something inside its own root — so nothing here is shared
 *  between them but the row markup. */
function StagingCard({ id, root, busy, onDelete, onClear }: {
  id: StagingRootId;
  root: StagingRoot;
  busy: boolean;
  onDelete: (e: StagingEntry) => void;
  onClear: () => void;
}) {
  const shown = root.entries.slice(0, STAGING_ROWS);

  return (
    <div className="rounded-md border border-border bg-panel/60 p-3 space-y-2">
      <div className="flex items-start justify-between gap-2 flex-wrap">
        <div className="min-w-0">
          <div className="text-xs font-semibold text-zinc-300">
            {id === "downloads" ? "Downloads" : "Incomplete"}
          </div>
          <div className="text-[10px] text-zinc-600 truncate" title={root.folder}>{root.folder}</div>
          <div className="text-[10px] text-zinc-500 mt-0.5">
            {!root.exists
              ? "folder not created yet"
              : root.count === 0
                ? "empty"
                : stagingTotals(root.count, root.bytes)}
            {/* the incomplete root holds slskd's in-flight leftovers, so its
                bytes are partial files — worth saying out loud before deleting */}
            {root.exists && id === "incomplete" && root.count > 0 && (
              <span className="text-zinc-600"> · partial bytes, not resumable</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {root.count > 0 && (
            <button
              className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
              disabled={busy}
              onClick={onClear}
              title={`Delete every entry in ${root.folder} — the folder itself stays`}
            >
              <Trash2 className="h-3 w-3" /> Clear all
            </button>
          )}
        </div>
      </div>
      {root.entries.length > 0 && (
        <div className="space-y-0.5 max-h-[320px] overflow-auto stagger">
          {shown.map((e) => (
            <div key={e.name} className="flex items-center gap-2 px-2 py-1 rounded hover:bg-white/[0.04] text-xs">
              <div className="flex-1 min-w-0 truncate text-zinc-200" title={e.name}>{e.name}</div>
              {e.partial && (
                <span className="chip text-[9px] bg-amber-950/60 text-amber-300 border border-amber-900" title="slskd is still writing this one — a leftover, not a finished result">partial</span>
              )}
              {e.album && (
                <span className="chip text-[9px] bg-raise border border-border text-zinc-400" title="Holds audio, so it can be imported into the library">album</span>
              )}
              <span className="hidden sm:block text-zinc-500 w-8 text-right shrink-0" title={`${e.files} file(s)`}>{e.files} f</span>
              <span className="text-zinc-500 w-14 sm:w-16 text-right shrink-0">{fmtSize(e.bytes)}</span>
              <button
                className="btn-ghost !px-2 sm:!px-1.5 !py-0.5 text-[11px] shrink-0 tap"
                disabled={busy}
                onClick={() => onDelete(e)}
                title={`Delete ${e.name} from ${root.folder}`}
              >
                <Trash2 className="h-3 w-3" />
                <span className="hidden sm:inline">Delete</span>
              </button>
            </div>
          ))}
          {root.entries.length > shown.length && (
            <div className="text-[10px] text-zinc-600 px-2 py-1">
              +{root.entries.length - shown.length} more — the totals above cover everything
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** "Staging on disk" — the two folders slskd writes into, with their real
 *  contents. Deliberately separate from DownloadsPanel above: that one lists
 *  slskd's transfer HISTORY (empty while the daemon is stopped), this one lists
 *  the bytes on disk, which is what has to be cleaned up. */
function StagingPanel() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  // No refetch interval: the server sizes every entry recursively, so polling
  // would walk the whole staging tree on a timer. Mount + every action is
  // enough — this panel is the thing that changes it.
  const { data, isError, refetch } = useQuery({
    queryKey: ["soulseekStaging"],
    queryFn: api.soulseekStaging,
  });

  /** The same caches the existing download routes bust — the transfer list and
   *  the Downloads page both describe the same bytes, so a delete here that
   *  left them stale would show a folder that no longer exists. */
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["soulseekStaging"] });
    qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
    qc.invalidateQueries({ queryKey: ["downloads"] });
  };

  const remove = async (id: StagingRootId, root: StagingRoot, e: StagingEntry) => {
    if (!window.confirm(
      `Delete ${e.name} from ${root.folder}?\n\n` +
      `${fmtSize(e.bytes)} · ${e.files} file(s), permanently.`
    )) return;
    setBusy(true);
    try {
      const r = await api.soulseekStagingDelete(id, e.name);
      toast(`${e.name} deleted${r.freed ? ` · ${fmtSize(r.freed)} freed` : ""}`);
      refresh();
    } catch (err) {
      toast.error(String(err));
    } finally {
      setBusy(false);
    }
  };

  const clear = async (id: StagingRootId, root: StagingRoot) => {
    if (!window.confirm(
      `Delete ${stagingTotals(root.count, root.bytes)} from ${root.folder}?\n\n` +
      (id === "incomplete"
        ? "These are partial bytes slskd left behind — they cannot be resumed after this."
        : "The whole staging folder goes; the folder itself stays.")
    )) return;
    setBusy(true);
    try {
      const r = await api.soulseekStagingClear(id);
      const freed = r.freed ? ` · ${fmtSize(r.freed)} freed` : "";
      const refused = r.failed.length ? ` · ${r.failed.length} refused` : "";
      toast(`Cleared ${r.cleared} ${r.cleared === 1 ? "entry" : "entries"}${freed}${refused}`);
      refresh();
    } catch (err) {
      toast.error(String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Staging on disk</div>
        <span className="text-[10px] text-zinc-600">
          what is actually in slskd's two staging folders — deleting here touches the disk, not the transfer list
        </span>
      </div>
      {isError ? (
        <div className="text-xs text-amber-300">
          Could not read the staging folders — the server may be restarting (the walk can also take a
          while on a large queue).{" "}
          <button className="btn-ghost !py-0.5 text-xs tap" onClick={() => refetch()}>
            Retry
          </button>
        </div>
      ) : !data ? (
        <PageLoading />
      ) : (
        <div className="grid gap-2 lg:grid-cols-2">
          {(["downloads", "incomplete"] as const).map((id) => (
            <StagingCard
              key={id}
              id={id}
              root={data[id]}
              busy={busy}
              onDelete={(e) => remove(id, data[id], e)}
              onClear={() => clear(id, data[id])}
            />
          ))}
        </div>
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
          {[...sharingNow, ...past].slice(0, 60).map((f) => (
            <div key={`${f.username}\u0000${f.dir}\u0000${f.filename}`} className="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-white/[0.04] text-xs">
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

/** `<music>/.mlo/downloads` — slskd's staging area as a triage list: pick the
 *  entries worth keeping and import them (moving each into the library and
 *  running the configured import chain on it), or delete them.
 *
 *  Everything Soulseek pulls down lands here and stays until one of those two
 *  happens; entries are folders (usually one album each) or loose files, and
 *  selecting several acts on all of them at once. The same bytes also show up
 *  under "Staging on disk" below — that one clears the in-flight leftovers,
 *  this one is where a finished album goes into the library. */
function StagingImportPanel() {
  const qc = useQueryClient();
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  /** The bulk job an import of 2+ entries runs (null = none). */
  const [bulkJob, setBulkJob] = useState<ImportBulkJob | null>(null);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["downloads"],
    queryFn: api.downloads,
    refetchInterval: 15000,
  });

  // Poll the bulk queue while it runs; done/failed stops the poll.
  useEffect(() => {
    if (bulkJob?.status !== "running") return;
    const timer = setInterval(() => {
      api.importBulkStatus()
        .then((job) => {
          setBulkJob(job);
          if (job.status !== "running") {
            qc.invalidateQueries({ queryKey: ["downloads"] });
            qc.invalidateQueries({ queryKey: ["library"] });
          }
        })
        .catch(() => setBulkJob(null)); // server restarted: nothing to poll
    }, 1500);
    return () => clearInterval(timer);
  }, [bulkJob?.status, qc]);

  const entries: DownloadEntry[] = data?.entries ?? [];
  const totals = useMemo(
    () => ({
      albums: entries.filter((e) => e.album).length,
      partial: entries.filter((e) => e.partial).length,
    }),
    [entries]
  );

  const toggle = (name: string) =>
    setSel((s) => {
      const next = new Set(s);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const selected = [...sel];
  /** Entries that exist in the current listing — a stale selection (a file
   *  that vanished between renders) must never be sent to the server. */
  const liveSelection = selected.filter((n) => entries.some((e) => e.name === n));

  const after = (msg: string) => {
    setSel(new Set());
    setConfirmDelete(false);
    qc.invalidateQueries({ queryKey: ["downloads"] });
    qc.invalidateQueries({ queryKey: ["library"] });
    toast(msg);
  };

  const doImport = async () => {
    if (!liveSelection.length || !data?.folder) return;
    setBusy(true);
    try {
      // ONE entry or several: same route. The bulk queue moves each entry into
      // the library AND runs the configured import chain on it — a single
      // entry used to take the move-only route and silently skip the chain.
      const job = await api.importBulk(
        liveSelection.map((name) => ({ path: `${data.folder}/${name}`, move: true }))
      );
      if (job.ok && job.job) {
        setBulkJob(job.job);
        setSel(new Set());
        toast(`Importing ${liveSelection.length} entr${liveSelection.length === 1 ? "y" : "ies"} — progress below`);
      } else {
        toast(`Queue import: ${job.error ?? "could not start"}`);
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const bulkFailed = (bulkJob?.items ?? []).filter((r) => r.status === "failed");

  const doDelete = async () => {
    if (!liveSelection.length) return;
    setBusy(true);
    try {
      const res = await api.downloadsDelete(liveSelection);
      after(
        res.failed.length
          ? `Deleted ${res.deleted.length} (${fmtSize(res.freed)}) — ${res.failed.length} failed: ${res.failed.map((f) => f.name).join(", ")}`
          : `Deleted ${res.deleted.length} entr${res.deleted.length === 1 ? "y" : "ies"} — freed ${fmtSize(res.freed)}`
      );
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const allSelected = entries.length > 0 && liveSelection.length === entries.length;

  return (
    <div className="panel">
      <div className="flex items-center justify-between gap-2 flex-wrap mb-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
          <FolderInput className="h-3.5 w-3.5" /> Import to library
          <span className="text-[10px] font-mono normal-case text-zinc-400">
            {data?.count ?? 0} entr{(data?.count ?? 0) === 1 ? "y" : "ies"} · {fmtSize(data?.bytes ?? 0)}
            {totals.albums > 0 && ` · ${totals.albums} look like albums`}
            {totals.partial > 0 && ` · ${totals.partial} in-flight`}
          </span>
        </div>
        <button
          className="btn-ghost !py-1 text-xs tap"
          onClick={() => refetch()}
          disabled={isFetching}
          title="Re-read the downloads folder"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> Refresh
        </button>
      </div>

      <div className="space-y-3">
        {data?.folder && (
          <div className="text-[11px] text-zinc-600 font-mono truncate" title={data.folder}>
            {data.folder}
          </div>
        )}

        {/* Bulk import (2+ entries): per-album progress of the queue. */}
        {bulkJob?.status === "running" && (
          <div className="rounded-md border border-border bg-panel/60 p-2.5 space-y-1.5">
            <div className="flex items-center gap-2 text-xs">
              <span className="flex-1 truncate text-zinc-300" title={bulkJob.label}>
                {bulkJob.label || "Importing…"}
              </span>
              <span className="text-zinc-500 font-mono tabular-nums shrink-0">
                {fmtCounts(bulkJob.done ?? 0, bulkJob.total ?? 0)}
              </span>
            </div>
            <div className="h-1.5 rounded-sm bg-raise overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-accent to-accent-soft transition-all duration-300"
                style={{
                  width: `${bulkJob.total ? Math.min(100, ((bulkJob.done ?? 0) / bulkJob.total) * 100) : 0}%`,
                }}
              />
            </div>
            <div className="text-[10px] text-zinc-500">
              Importing moves each entry into the library and runs the configured import chain on it.
            </div>
          </div>
        )}

        {bulkFailed.length > 0 && (
          <div className="rounded-md border border-red-900/60 bg-red-950/30 p-2.5 text-xs text-red-300 space-y-0.5">
            {bulkFailed.map((r) => (
              <div key={r.path} className="truncate" title={`${r.path} — ${r.error}`}>
                {r.path.split(/[\\/]/).pop()} — {r.error}
              </div>
            ))}
          </div>
        )}

        {/* Nothing to work with: the folder is created by the app on first use,
            so an absent one is normal on a fresh install, not an error. */}
        {!isLoading && !data?.exists && (
          <EmptyState
            title="No downloads folder yet"
            hint="It is created the first time Soulseek saves something."
          />
        )}

        {data?.exists && entries.length === 0 && (
          <EmptyState
            title="Nothing downloaded"
            hint="Soulseek downloads land here, then you import or delete them from this tab."
          />
        )}

        {entries.length > 0 && (
          <div className="flex items-center gap-2 flex-wrap">
            <button
              className="btn-ghost !py-1 text-xs tap"
              onClick={() => setSel(allSelected ? new Set() : new Set(entries.map((e) => e.name)))}
            >
              {allSelected ? "Select none" : "Select all"}
            </button>
            {liveSelection.length > 0 && (
              <span className="text-xs text-zinc-400">{liveSelection.length} selected</span>
            )}
            <div className="flex flex-wrap items-center gap-2 ml-auto justify-end">
              <button
                className="btn-primary !py-1 text-xs tap"
                onClick={doImport}
                disabled={busy || !liveSelection.length}
                title={
                  liveSelection.length > 1
                    ? "Move the selected entries into the library and run the import chain on each"
                    : "Move the selected entry into the library and run the import chain on it"
                }
              >
                <FolderInput className="h-3.5 w-3.5" />{" "}
                {liveSelection.length > 1 ? `Import ${liveSelection.length} (queue)` : "Import to library"}
              </button>
              {confirmDelete ? (
                <>
                  <span className="text-xs text-amber-300">Delete permanently?</span>
                  <button className="btn-danger !py-1 text-xs tap" onClick={doDelete} disabled={busy}>
                    <Trash2 className="h-3.5 w-3.5" /> Yes, delete
                  </button>
                  <button className="btn-ghost !py-1 text-xs tap" onClick={() => setConfirmDelete(false)} disabled={busy}>
                    <X className="h-3.5 w-3.5" /> Cancel
                  </button>
                </>
              ) : (
                <button
                  className="btn-danger !py-1 text-xs tap"
                  onClick={() => setConfirmDelete(true)}
                  disabled={busy || !liveSelection.length}
                  title="Delete the selected entries from disk — this cannot be undone"
                >
                  <Trash2 className="h-3.5 w-3.5" /> Delete
                </button>
              )}
            </div>
          </div>
        )}

        <div className="space-y-1.5">
          {entries.map((e) => {
            const on = sel.has(e.name);
            return (
              <label
                key={e.name}
                className={`flex items-center gap-3 bg-card rounded-lg border px-3 py-2 cursor-pointer ${
                  on ? "border-accent/60" : "border-border"
                } ${e.partial ? "opacity-60" : ""}`}
                title={e.partial ? "Still downloading or a scratch file — leave it alone" : e.name}
              >
                <input type="checkbox" checked={on} onChange={() => toggle(e.name)} />
                {e.dir ? (
                  <Disc3 className="h-4 w-4 text-zinc-500 shrink-0" />
                ) : (
                  <FolderOpen className="h-4 w-4 text-zinc-500 shrink-0" />
                )}
                <span className="flex-1 min-w-0">
                  <span className="block truncate text-sm text-zinc-200">{e.name}</span>
                  <span className="block text-[11px] text-zinc-500">
                    {fmtSize(e.bytes)}
                    {e.dir && ` · ${e.files} file(s)`}
                    {e.audio > 0 && ` · ${e.audio} audio`}
                    {e.images > 0 && ` · ${e.images} image(s)`}
                    {!e.album && e.audio === 0 && " · no audio"}
                  </span>
                </span>
                {e.partial && (
                  <span className="chip bg-amber-950/40 text-amber-300 border border-amber-900 shrink-0">
                    <AlertTriangle className="h-3 w-3" /> in-flight
                  </span>
                )}
                {!e.partial && e.album && (
                  <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800 shrink-0">
                    importable
                  </span>
                )}
                {!e.partial && !e.album && (
                  <span className="chip bg-raise border border-border text-zinc-500 shrink-0">no audio</span>
                )}
              </label>
            );
          })}
        </div>

        {entries.length > 0 && (
          <div className="text-[10px] text-zinc-600">
            Import moves an entry into the library as its own album named after the folder and runs the
            configured import chain on it (Settings → Import); several entries are just a queue of that.
            Delete removes it from disk for good — nothing here is copied to the trash bin first.
          </div>
        )}
      </div>
    </div>
  );
}
