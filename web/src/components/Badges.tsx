import { Link } from "react-router-dom";
import { Check, X, Disc3, CircleAlert, ArrowDown, Loader2 } from "lucide-react";
import { useCachedPaths } from "../lib/mediaCache";
import { useI18n, type MessageKey } from "../lib/i18n";
import type { AdvisoryFetchResult } from "../api";
import type { Album } from "../types";

/** The translate function `useI18n` hands out — what the pending sentences
 *  below are built with (they are shared by every surface, so the component
 *  that draws a row does not have to know how to say them). */
type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

/** The one condensed grade verdict: a small check (pass) or cross (fail)
 * and nothing else — grading stays out of the way; `score` (the old
 * percentage) survives as a hover detail. */
export function GradeBadge({
  pass,
  score,
  size = "md",
}: {
  pass: boolean;
  score?: number | null;
  size?: "sm" | "md";
}) {
  // One word for the verdict. The tooltip used to append `audit REAL` /
  // `audit FAKE` — the app's internal name for the CD verdict, which a reader
  // hovering a row should not have to learn (the audit chip in the track
  // details says it where there is room to say it in full). The failing
  // percentage stays: it is the number behind the cross.
  const title = pass
    ? "Pass"
    : score != null
      ? `Fail · ${score}% of checks passed`
      : "Fail";
  return (
    <span
      className={`inline-flex items-center shrink-0 ${
        pass ? "text-emerald-600/70" : "text-red-400/90"
      }`}
      title={title}
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
 *  value in its OWN capitalization ("CD", "Digital", "SACD", "Vinyl"…). */
export function mediaShort(media: string | null | undefined): string | null {
  const m = (media ?? "").trim();
  if (!m) return null;
  const first = (m.split(/[\s/;,]+/)[0] ?? "").slice(0, 10);
  return first || null;
}

/** Every country a release was released in, in the order the tag lists them.
 *  RELEASECOUNTRY is one value on most files and a LIST on the ones tagged
 *  from a release group's events ("US; CA; JP" — the separators beets,
 *  Picard and foobar write), so the badge it feeds has to cope with several
 *  rather than print the raw string as if it were one place. */
export function releaseCountries(value: string | null | undefined): string[] {
  const out: string[] = [];
  for (const part of (value ?? "").split(/[;,/]/)) {
    const c = part.trim();
    // A handful of codes at most, so the linear scan IS the dedupe.
    if (!c || out.some((x) => x.toUpperCase() === c.toUpperCase())) continue;
    out.push(c);
  }
  return out;
}

/** The one media label every album/release badge wears: the medium, then the
 *  release countries — "CD · US, CA". A card and the page header it opens
 *  name the same pressing the same way, and an album released in several
 *  countries says so instead of showing a single code it picked. */
export function mediaCountryLabel(
  media: string | null | undefined,
  country: string | null | undefined
): string | null {
  const parts = [mediaShort(media), releaseCountries(country).join(", ")].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
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

/** The advisory source ids in a reader's words. The server reports WHICH stage
 *  spoke — a provider that stated the value, the AI when no source stated
 *  anything ("ai-lyrics" when it read the words, "ai" when the file held
 *  none), the instrumental rule, the ladder's last resort ("fallback") or
 *  "existing-tag" for a value echoed off the file. Those ids are the server's
 *  vocabulary, not a user's: a bare "existing-tag" said nothing about the fact
 *  that nobody was asked at all. An id this map does not know keeps the
 *  server's own spelling — it is evidence, never a guess to prettify. */
const ADVISORY_SOURCE_WORDS: Record<string, string> = {
  "existing-tag": "the file's own tag, not re-checked",
  "deezer-isrc": "Deezer (ISRC)",
  "spotify-isrc": "Spotify (ISRC)",
  "apple-album": "Apple (album editions)",
  "itunes-song": "iTunes (song search)",
  "discogs-parental": "Discogs (parental advisory)",
  "youtube-age": "YouTube (age gate)",
  "ai-lyrics": "the AI's read of the lyrics",
  ai: "the AI (no lyrics in the file)",
  instrumental: "the track is instrumental",
  fallback: "the configured fallback — no source stated a value",
};

/** What the reply's per-path `status` adds to a line, for the two states the
 *  value's own provenance does not already say. `written` needs no words (the
 *  value and the source that stated it are the whole story) and `existing` is
 *  spelled out by the "existing-tag" source above; what a reader cannot see
 *  otherwise is a re-check the sources AGREED with, and a write the gate
 *  refused. */
const ADVISORY_STATUS_SUFFIX: Record<string, string> = {
  unchanged: "re-checked — the sources state what the file already carries",
  gated: "left alone — writing ITUNESADVISORY is off for this file type",
};

/** "1 (explicit) · deezer-isrc, apple-album" — one line: the value, the
 *  providers behind it, and (when the caller passes it) what the run that
 *  answered DID with this file's value. A value nobody stated reads "unknown",
 *  and "source unknown" is reserved for a reply that truly reported no
 *  provenance: a value echoed off the file reads "the file's own tag, not
 *  re-checked", which says WHO decided it (the user, or an earlier run)
 *  instead of implying a source answered and its answer was lost. */
export function advisoryLine(
  value: string | number | null | undefined,
  sources: string[],
  status?: string | null
): string {
  const v = String(value ?? "").trim();
  const list = sources.length
    ? sources.map((s) => ADVISORY_SOURCE_WORDS[s] ?? s).join(", ")
    : "source unknown";
  const state = status ? ADVISORY_STATUS_SUFFIX[status] : "";
  return `${v ? `${v} (${advisoryLabel(v)})` : "unknown"} · ${list}${state ? ` · ${state}` : ""}`;
}

/** "instrumental · lyrics-present" — INSTRUMENTAL is 0/1 only; anything else
 *  (including an absent tag) is unknown, not a value. */
export function instrumentalLine(value: string | number | null | undefined, sources: string[]): string {
  const v = String(value ?? "").trim();
  const list = sources.length ? sources.join(", ") : "source unknown";
  return `${v === "1" ? "instrumental" : v === "0" ? "not instrumental" : "unknown"} · ${list}`;
}

/** What one advisory fetch amounts to, in the words every surface reports it
 *  with: the per-track values it wrote, the album tag it DERIVED from them,
 *  and — when the write gate refused files — that, instead of a bare "0
 *  re-rated" that reads like a silent success.
 *
 *  The reply's own `status` map is what makes `updated == 0` legible: a fetch
 *  that wrote nothing did one of THREE different things per track — echoed a
 *  value the file already carried without asking anyone (`existing`), asked and
 *  found the sources agreeing with what is stored (`unchanged`), or had the
 *  write gate refuse it (`gated`) — and each is a different thing to tell a
 *  user. Nothing-written is never rendered as success. */
export function advisoryOutcome(reply: AdvisoryFetchResult | null | undefined): string {
  if (!reply) return "no reply";
  // A reply that wrote NOTHING is the one case where the server's own reason
  // leads: the counts below would otherwise read as "the sources answered".
  if (reply.skipped && !reply.updated && !reply.album_updated) {
    return `nothing written — ${reply.skipped}`;
  }
  const statuses = Object.values(reply.status ?? {});
  const howMany = (state: string) => statuses.filter((s) => s === state).length;
  const existing = howMany("existing");
  const unchanged = howMany("unchanged");
  const gated = howMany("gated");
  const parts: string[] = [];
  if (reply.updated) parts.push(`${reply.updated} track(s) re-rated`);
  if (existing) {
    parts.push(`${existing} track(s) already rated — the file's own value was kept and no source`
      + ` was asked`);
  }
  if (unchanged) {
    parts.push(`${unchanged} track(s) re-checked — the sources state what the file already carries`);
  }
  if (gated) parts.push(`${gated} left alone — writing ITUNESADVISORY is off for their file type`);
  // No status at all (a server predating the map) still must not print a bare
  // "0 re-rated": what is knowable is that nothing was written.
  if (!parts.length) parts.push("nothing written — the reply reported no per-track outcome");
  if (reply.album_updated) {
    const values = Object.values(reply.albums ?? {}).map(String);
    parts.push(`album tag ${values.filter((v, i) => values.indexOf(v) === i).join("/")}`
      + ` on ${reply.album_updated} track(s)`);
  }
  // The album tag answers to its own switch (script 8's derivation), so a
  // fetch can rate every track and still leave the album tag refused.
  if (reply.album_gated) {
    parts.push(`${reply.album_gated} album tag(s) left alone — Auto Album Advisory is off`);
  }
  if (reply.skipped) parts.push(reply.skipped);
  return parts.join(" · ");
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

export function EmptyState({ title, hint, action, onAction }: {
  title: string;
  hint?: string;
  /** Dead-end pages get a way back — same affordance the router 404 gives. */
  action?: { label: string; to: string };
  /** The same affordance for a state that is fixed IN PLACE: retry a query
   *  that failed, clear a builder that matched nothing. A page with one of
   *  these must never leave a reader with a dead end and no way out. */
  onAction?: { label: string; onClick: () => void; disabled?: boolean };
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-24 text-zinc-500">
      <Disc3 className="h-9 w-9 opacity-30" />
      <div className="text-sm font-medium text-zinc-400">{title}</div>
      {hint && <div className="text-xs text-zinc-600 max-w-sm text-center break-words">{hint}</div>}
      {action && (
        <Link to={action.to} className="btn-ghost !py-1.5 text-xs mt-2 tap">
          {action.label}
        </Link>
      )}
      {onAction && (
        <button
          className="btn-ghost !py-1.5 text-xs mt-2 tap"
          disabled={onAction.disabled}
          onClick={onAction.onClick}
        >
          {onAction.label}
        </button>
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

/** The downloaded mark for a track title: a small down arrow, the same
 *  download family the download buttons wear, rendered only while the audio
 *  is in the offline cache (lib/mediaCache) — a track that was never
 *  downloaded gets NO mark, so the badge means "you can play this without the
 *  server" and nothing else.
 *
 *  It used to be a green circled check, which is what a passing grade looks
 *  like two columns to the left: the reader saw two near-identical ticks and
 *  could tell neither what was graded from what was downloaded (#39). The
 *  grade keeps the check; the download takes the arrow. */
export function CachedMark({ path, size = "sm" }: { path: string; size?: "sm" | "md" }) {
  const cached = useCachedPaths();
  if (!cached.has(path)) return null;
  return (
    <span
      role="img"
      aria-label="Downloaded"
      title="Downloaded"
      className="shrink-0 inline-flex text-emerald-500"
    >
      <ArrowDown className={size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4"} />
    </span>
  );
}

/** The pending marker's fields — a framework album's own block, exactly as the
 *  server stamps it on every album-shaped row (`server/library.py`). */
export type PendingFields = Pick<Album, "pending" | "pending_reason" | "wish" | "wish_id">;

/** What a framework album's marker SAYS: the short label a row draws and the
 *  full sentence its tooltip carries.
 *
 *  One function, so the grid, the artist page, the query rows, Home and the
 *  album page can never describe the same album differently. Every part of it
 *  is the server's own data: `pending_reason` is what the folder is waiting
 *  for, and `wish` is the queue's state — how many attempts ran, the reason a
 *  run left behind, and whether another search is even coming. None for a
 *  complete album: there is no marker to draw and nothing to say.
 */
export function pendingSummary(album: PendingFields, t: Translate): { short: string; state: string; full: string } | null {
  if (!album?.pending) return null;
  const w = album.wish;
  const state = !w
    ? t("pending.no_search")
    : w.status === "not_found"
      ? t("pending.not_found")
      : w.status === "failed"
        ? (w.reason || t("pending.failed"))
        : w.terminal || w.due_in == null
          ? (w.reason || t("pending.no_search"))
          : w.attempts === 0 && w.status === "wanted"
            // Recorded and waiting its turn: nothing has searched it yet, so
            // "attempt 1" would claim a run that has not happened.
            ? t("pending.queued")
            : t("pending.attempt", {
                n: w.attempts + 1,
                m: Math.max(1, Math.round((w.due_in ?? 0) / 60)),
              });
  return {
    short: t("pending.short"),
    state,
    full: [t("pending.title"), album.pending_reason, state].filter(Boolean).join(" · "),
  };
}

/** A framework album's mark: the same shape the grading verdict uses (a small
 *  dot whose sentence is on hover and read out to a screen reader), amber
 *  while a search is still coming and red once nothing will search the folder
 *  again — "needs attention" in the app's own tokens, not a second palette.
 *
 *  It renders NOTHING for a complete album, so any row can mount it
 *  unconditionally, and it is a different fact from the lock chip beside it: a
 *  folder can be pending (no audio yet) and locked (a job holding what is
 *  there) at once, and neither mark replaces the other. */
export function PendingMark({ album, size = "sm", label = false }: {
  album: PendingFields;
  size?: "sm" | "md";
  /** Draw the short state beside the dot, for a row with room to say it. */
  label?: boolean;
}) {
  const { t } = useI18n();
  const note = pendingSummary(album, t);
  if (!note) return null;
  const stuck = !album.wish || !!album.wish.terminal || album.wish.status === "failed";
  return (
    <span
      className="inline-flex items-center gap-1 shrink-0"
      role="img"
      aria-label={note.full}
      title={note.full}
    >
      <span
        className={`${size === "sm" ? "h-1.5 w-1.5" : "h-2 w-2"} rounded-full shrink-0 ${
          stuck ? "bg-red-500/80" : "bg-amber-400/90 animate-pulse"
        }`}
      />
      {label && (
        <span className={`text-[10px] ${stuck ? "text-red-300/80" : "text-amber-300/80"}`}>
          {note.short}
        </span>
      )}
    </span>
  );
}