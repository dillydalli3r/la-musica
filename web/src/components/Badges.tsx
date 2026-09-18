import { Link } from "react-router-dom";
import { Check, X, Disc3, CircleAlert, CheckCircle2, Loader2 } from "lucide-react";
import { useCachedPaths } from "../lib/mediaCache";

/** The one condensed grade verdict: a small check (pass) or cross (fail)
 * and nothing else — grading stays out of the way; `score` (the old
 * percentage) and `audit` survive as hover details. */
export function GradeBadge({
  pass,
  score,
  audit,
  size = "md",
}: {
  pass: boolean;
  score?: number | null;
  audit?: string | null;
  size?: "sm" | "md";
}) {
  const detail: string[] = [];
  if (score != null && !pass) detail.push(`${score}% of checks passed`);
  const a = (audit ?? "").trim().toUpperCase();
  if (a) detail.push(`audit ${a}`);
  return (
    <span
      className={`inline-flex items-center shrink-0 ${
        pass ? "text-emerald-600/70" : "text-red-400/90"
      }`}
      title={detail.length ? detail.join(" · ") : pass ? "All checks passed" : "Grading failed — see details"}
    >
      {pass ? (
        <Check className={size === "sm" ? "h-3 w-3" : "h-3.5 w-3.5"} />
      ) : (
        <X className={size === "sm" ? "h-3 w-3" : "h-3.5 w-3.5"} />
      )}
    </span>
  );
}

export function AuditBadge({ audit, size = "md" }: { audit: string | null; size?: "sm" | "md" }) {
  const cls = size === "sm" ? "text-[9px]" : "text-[10px]";
  switch ((audit || "").toUpperCase()) {
    case "REAL":
      return <span className={`${cls} font-mono text-emerald-600/60`} title="Audio audit: REAL">REAL</span>;
    case "FAKE":
      return <span className={`${cls} font-mono text-red-400/80`} title="Audio audit: FAKE">FAKE</span>;
    case "MIX":
      return <span className={`${cls} font-mono text-amber-400/70`} title="Audio audit: MIX">MIX</span>;
    default:
      return <span className={`${cls} font-mono text-zinc-700`} title="Not audited yet">AUDIT —</span>;
  }
}

export function MediaChip({ media }: { media: string | null | undefined }) {
  if (!media) return null;
  // Colorless by design: media type is metadata, not a status — the same
  // neutral look everywhere (album header, tables, cards).
  return <span className="chip bg-zinc-800/70 text-zinc-300 border border-border">{media}</span>;
}

/** Compact media label for album cards/icons: the first word of the media
 * value in its OWN capitalization ("CD", "Digital", "SACD", "Vinyl"…). */
export function mediaShort(media: string | null | undefined): string | null {
  const m = (media ?? "").trim();
  if (!m) return null;
  const first = (m.split(/[\s/;,]+/)[0] ?? "").slice(0, 10);
  return first || null;
}

export function AdvisoryBadge({ value }: { value: string | null | undefined }) {
  if (value === "1")
    return <span className="chip bg-red-900/50 text-red-300 border border-red-900">EXPLICIT</span>;
  if (value === "2")
    return <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-900">CLEAN</span>;
  return null;
}

/** ITUNESADVISORY in words for the provenance readouts — the same three
 *  states the badges draw. Anything else is unknown: a missing advisory is
 *  "unrated", never 0/clean. */
const ADVISORY_LABELS: Record<string, string> = {
  "0": "not explicit",
  "1": "explicit",
  "2": "clean edition",
};

export function advisoryLabel(value: string | number | null | undefined): string {
  return ADVISORY_LABELS[String(value ?? "").trim()] ?? "unknown";
}

/** "1 (explicit) · deezer-isrc, apple-album" — one line: the value and the
 *  providers behind it. A value nobody stated reads "unknown", and provenance
 *  that was never reported reads "source unknown": neither is guessed. */
export function advisoryLine(value: string | number | null | undefined, sources: string[]): string {
  const v = String(value ?? "").trim();
  const list = sources.length ? sources.join(", ") : "source unknown";
  return `${v ? `${v} (${advisoryLabel(v)})` : "unknown"} · ${list}`;
}

/** "instrumental · lyrics-present" — INSTRUMENTAL is 0/1 only; anything else
 *  (including an absent tag) is unknown, not a value. */
export function instrumentalLine(value: string | number | null | undefined, sources: string[]): string {
  const v = String(value ?? "").trim();
  const list = sources.length ? sources.join(", ") : "source unknown";
  return `${v === "1" ? "instrumental" : v === "0" ? "not instrumental" : "unknown"} · ${list}`;
}

/** iTunes-style advisory mark: a tiny boxed letter shown beside track and
 * album titles ("square text symbol"). Drawn in CSS so it renders exactly
 * the same everywhere — emoji squared-letter glyphs vary by platform. */
export function AdvisoryMark({ value, size = "sm" }: { value: string | null | undefined; size?: "sm" | "md" }) {
  const box = size === "sm" ? "h-3.5 w-3.5 text-[8px] rounded-[3px]" : "h-4 w-4 text-[9px] rounded-[4px]";
  if (value === "1")
    return (
      <span
        className={`inline-flex items-center justify-center shrink-0 font-bold leading-none bg-red-600/85 text-white ${box}`}
        title="Explicit"
      >
        E
      </span>
    );
  if (value === "2")
    return (
      <span
        className={`inline-flex items-center justify-center shrink-0 font-bold leading-none border border-emerald-700/70 bg-emerald-900/40 text-emerald-300 ${box}`}
        title="Clean"
      >
        C
      </span>
    );
  return null;
}

export function InstrumentalBadge({ value }: { value: string | null | undefined }) {
  if (value === "1")
    return <span className="chip bg-zinc-800 text-zinc-400 border border-border">INSTRUMENTAL</span>;
  return null;
}

/** Linear grade meter (rectangular language — no progress rings): a slim
 * bar filled by the % of checks passed, quiet colors, details on hover. */
export function GradeBar({ pct, width = 64 }: { pct: number | null; width?: number }) {
  const v = Math.max(0, Math.min(100, pct ?? 0));
  const color = v >= 100 ? "bg-emerald-600/60" : v >= 80 ? "bg-amber-500/60" : "bg-red-500/70";
  return (
    <span
      className="inline-block h-1 rounded-sm bg-border/80 overflow-hidden align-middle shrink-0"
      style={{ width }}
      title={pct == null ? "Not graded" : `${pct}% of checks passed`}
    >
      <span className={`block h-full ${color}`} style={{ width: `${v}%` }} />
    </span>
  );
}

export function ScoreRing({ pct, size = 44 }: { pct: number | null; size?: number }) {  const r = (size - 6) / 2;
  const c = 2 * Math.PI * r;
  const v = pct ?? 0;
  const ok = v >= 100;
  const color = ok ? "#34d399" : v >= 80 ? "#fbbf24" : "#f87171";
  return (
    <svg width={size} height={size} className="-rotate-90">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#26262c" strokeWidth={5} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={5}
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={c - (c * v) / 100}
      />
      <text
        x="50%"
        y="50%"
        textAnchor="middle"
        dominantBaseline="central"
        className="rotate-90"
        style={{ transform: "rotate(90deg)", transformOrigin: "center" }}
        fill="#e8e8e8"
        fontSize={size / 4}
        fontWeight={600}
      >
        {pct === null ? "–" : v}
      </text>
    </svg>
  );
}

export function IssueList({ issues }: { issues: string[] }) {
  if (!issues?.length)
    return (
      <span className="text-xs text-emerald-400 flex items-center gap-1">
        <Check className="h-3 w-3" /> Clean
      </span>
    );
  return (
    <span className="text-xs text-red-300 flex items-center gap-1 truncate" title={issues.join(", ")}>
      <CircleAlert className="h-3 w-3 shrink-0" />
      {issues.join(", ")}
    </span>
  );
}

export function EmptyState({ title, hint, action }: {
  title: string;
  hint?: string;
  /** Dead-end pages get a way back — same affordance the router 404 gives. */
  action?: { label: string; to: string };
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-24 text-zinc-500">
      <Disc3 className="h-9 w-9 opacity-30" />
      <div className="text-sm font-medium text-zinc-400">{title}</div>
      {hint && <div className="text-xs text-zinc-600 max-w-sm text-center">{hint}</div>}
      {action && (
        <Link to={action.to} className="btn-ghost !py-1.5 text-xs mt-2">
          {action.label}
        </Link>
      )}
    </div>
  );
}

/** Page-level loading placeholder. Same geometry as EmptyState so a page
 *  does not jump when the payload arrives. */
export function PageLoading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-24 text-sm text-zinc-500">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label}
    </div>
  );
}

/** The downloaded mark for a track title: the same check the download button
 *  shows once the audio is in the offline cache (lib/mediaCache), rendered
 *  only while it is there — a track that was never downloaded gets NO mark,
 *  so the badge means "you can play this without the server" and nothing
 *  else. */
export function CachedMark({ path, size = "sm" }: { path: string; size?: "sm" | "md" }) {
  const cached = useCachedPaths();
  if (!cached.has(path)) return null;
  return (
    <span
      role="img"
      aria-label="Downloaded"
      title="Downloaded — plays without the server"
      className="shrink-0 inline-flex text-emerald-500"
    >
      <CheckCircle2 className={size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4"} />
    </span>
  );
}