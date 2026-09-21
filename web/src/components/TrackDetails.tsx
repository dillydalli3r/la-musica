import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, CircleAlert, Info, ExternalLink, Loader2, RefreshCw, ChevronDown, ChevronRight, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { api, answerSources, checkTrackValues, replyFor } from "../api";
import { toast } from "../store";
import type { AdvisoryFetchResult, CreditRow, InstrumentalFetchResult } from "../api";
import type { Track } from "../types";
import { fmtDuration, fmtTech } from "../lib/fmt";
import { trackRef } from "../lib/refs";
import { AuditBadge, GradeBadge, advisoryLine, advisoryOutcome, instrumentalLine } from "./Badges";
import TrackDownloadExport from "./TrackDownloadExport";
import Modal from "./Modal";
import { tagLabel, tagTooltip, useTagRegistry } from "../lib/tags";

/** The tags this modal gives their own row elsewhere (advisory, instrumental,
 *  AudioAuditor, the lyrics state) — a display choice, so they are kept out of
 *  the generic check list. Labels come from the registry. */
const DEDICATED_ROWS = ["GENRE", "ITUNESADVISORY", "INSTRUMENTAL", "MEDIA", "SOURCE", "AUDIOAUDITOR_OVERRIDE"];

/** Which tags the Song info table lists, in this order.
 *
 *  A display choice — which tags are worth a row in a compact modal — and
 *  nothing more: every LABEL comes from the registry (server/tags_registry.py),
 *  so this list can reorder or trim rows but cannot invent a name for a tag. */
const INFO_KEYS = [
  "ARTIST", "ALBUMARTIST", "ALBUM", "TRACKNUMBER", "DISCNUMBER", "DATE",
  "ORIGINALDATE", "GENRE", "MEDIA", "SOURCE", "RELEASETYPE", "RELEASECOUNTRY",
  "LABEL", "CATALOGNUMBER", "COMPOSER", "LYRICIST", "REMIXER", "COPYRIGHT",
  "ISRC", "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ALBUMID",
];

/** One row of a details table: `value` is whatever the row renders — a string,
 *  a link, a small control. */
export interface DetailItem {
  label: string;
  value: ReactNode;
  title?: string;
}

/** The bordered label/value table every details readout is built from (track
 *  details, album details): one renderer, so a further readout is a list of
 *  rows and never a second table implementation. */
export function DetailRows({ rows }: { rows: DetailItem[] }) {
  return (
    <div className="rounded-md border border-border overflow-hidden">
      <table className="w-full text-xs">
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.label}-${i}`}>
              <td className="px-2 py-1 text-zinc-500 w-28 align-top" title={r.title}>{r.label}</td>
              <td className="px-2 py-1 text-zinc-200 break-all">{r.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The uppercase section heading with its glyph — and an optional action on the
 *  right — that the details modals open their sections with. */
export function DetailSection({ icon: Icon, title, action, children }: {
  icon: LucideIcon; title: string; action?: ReactNode; children: ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">
        <Icon className="h-3.5 w-3.5" /> {title}
        {action ? <div className="ml-auto normal-case tracking-normal">{action}</div> : null}
      </div>
      {children}
    </div>
  );
}

/** Per-track song info + grading/audit detail modal (metadata, credits,
 * tech, lyrics, checks, verdicts). */
export default function TrackDetails({
  track,
  albumPath,
  onClose,
  messages,
}: {
  track: Track;
  albumPath: string;
  onClose: () => void;
  /** The album's own problem text for THIS track's failed checks, in the
   *  same order as `track.issues` — the grader reports the message on the
   *  album ("PATH: expected '…' (run organize)") and only the check CODE on
   *  the track, so the modal would otherwise list bare codes like "PATH"
   *  with nothing to act on. Callers that have no messages keep the codes. */
  messages?: string[];
}) {
  const issues: string[] = track.issues ?? [];
  const values = track.values ?? {};
  const tags = track.tags ?? {};
  const checkRows = Object.entries(values).filter(([k]) => !DEDICATED_ROWS.includes(k));
  const failKeys = new Set(issues.map((i) => i.toUpperCase()));
  const infoRows = INFO_KEYS.filter((key) => tags[key as keyof typeof tags]);
  // What the app knows about each of those tags — the label, and the prose the
  // registry carries for it (shown as the row's tooltip).
  const reg = useTagRegistry();
  const tech = [fmtTech(track.tech), track.tech.length ? fmtDuration(track.tech.length) : ""].filter(Boolean).join(" · ");
  const lyricsState = track.lyrics_embedded ? "embedded" : track.lyrics_lrc ? ".lrc sidecar" : "missing";
  const trackPath = track.path ?? "";
  // Credits are looked up only once the section is opened — the panel below
  // mounts on open, so the modal never waits on MusicBrainz.
  const [creditsOpen, setCreditsOpen] = useState(false);

  // Provenance exists only in the check endpoints' reply (they report which
  // provider stated each value); it is not readable off the file afterwards,
  // so until a check runs in this session it reads "source unknown".
  const [checked, setChecked] = useState<null | { adv?: AdvisoryFetchResult; inst?: InstrumentalFetchResult }>(null);
  const [checking, setChecking] = useState(false);
  const qc = useQueryClient();
  const checkPerTrack = async () => {
    setChecking(true);
    try {
      const { adv, inst, errors } = await checkTrackValues([trackPath]);
      setChecked({ adv: adv ?? undefined, inst: inst ?? undefined });
      if (errors.length) toast.error(errors.join(" · "));
      else toast(`Checked — ${advisoryOutcome(adv)}, ${inst?.updated ?? 0} instrumental value(s) written`);
      qc.invalidateQueries({ queryKey: ["album", albumPath] });
      qc.invalidateQueries({ queryKey: ["track-tags", trackPath] });
    } finally {
      setChecking(false);
    }
  };
  const advisory = advisoryLine(
    replyFor(checked?.adv?.values, trackPath) ?? tags.ITUNESADVISORY,
    answerSources(replyFor(checked?.adv?.answers, trackPath), replyFor(checked?.adv?.sources, trackPath))
  );
  const instrumental = instrumentalLine(
    replyFor(checked?.inst?.values, trackPath) ?? tags.INSTRUMENTAL,
    answerSources(replyFor(checked?.inst?.evidence, trackPath))
  );

  return (
    <Modal
      onClose={onClose}
      icon={ShieldCheck}
      title={track.tags?.TITLE ?? track.file}
      width="max-w-lg"
      bodyClass="px-5 py-5 space-y-4"
    >
      {/* ---- song info: metadata & credits ---- */}
      <DetailSection
        icon={Info}
        title="Song info"
        action={
          <button
            className="btn-ghost !py-0.5 !px-1.5 text-[10px] font-normal"
            onClick={checkPerTrack}
            disabled={checking}
            title="Ask the configured sources for this track's advisory + INSTRUMENTAL and write what they state"
          >
            {checking ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            Check advisory + instrumental
          </button>
        }
      >
        {tech && <div className="text-[11px] font-mono text-zinc-500 mb-1.5">{tech}</div>}
        <DetailRows
          rows={[
            { label: "Title", value: track.tags?.TITLE ?? track.file },
            ...infoRows.map((key) => ({
              label: tagLabel(reg, key),
              title: tagTooltip(reg, key),
              value: String(tags[key as keyof typeof tags]),
            })),
            { label: "Lyrics", value: lyricsState },
            { label: "Advisory", value: advisory },
            { label: "Instrumental", value: instrumental },
            { label: "AudioAuditor", value: <AuditOverride path={track.path} current={track.audit} /> },
            { label: "Path", value: track.path ?? albumPath },
          ]}
        />
      </DetailSection>

      {/* ---- credits: performers & roles, looked up only when opened ---- */}
      <div>
        <button
          className="flex items-center gap-1.5 w-full text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5"
          onClick={() => setCreditsOpen((v) => !v)}
          aria-expanded={creditsOpen}
          title="Performers, instruments and studio roles for this track"
        >
          <Users className="h-3.5 w-3.5" /> Credits
          {creditsOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
        {creditsOpen && <CreditsPanel path={trackPath || undefined} tags={tags} />}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <GradeBadge pass={!issues.length} score={issues.length ? 0 : 100} />
        <AuditBadge audit={track.audit} />
        {track.log_grade != null && track.log_grade !== "" && (
          <span className="chip bg-raise border border-border text-zinc-300">log {track.log_grade}/100</span>
        )}
        {track.accuraterip_status && (
          <span className="chip bg-raise border border-border text-zinc-300">AR {track.accuraterip_status}</span>
        )}
        {track.checksum_status && (
          <span className="chip bg-raise border border-border text-zinc-300">CS {track.checksum_status}</span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <TrackDownloadExport path={track.path} title={track.tags?.TITLE ?? track.file} />
        {/* jump to the track's own page for full tag editing */}
        <Link
          to={trackRef({ path: track.path, tags: { MUSICBRAINZ_TRACKID: track.tags?.MUSICBRAINZ_TRACKID } })}
          className="btn-ghost !py-1.5 text-xs tap"
          title="Open the track page"
        >
          <ExternalLink className="h-3.5 w-3.5" /> Track page
        </Link>
      </div>

      {issues.length > 0 && (
        <div>
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">
            <CircleAlert className="h-3.5 w-3.5 text-red-400" /> Failed checks ({issues.length})
          </div>
          <ul className="space-y-1">
            {issues.map((iss, i) => (
              <li key={i} className="text-xs text-red-300/90 bg-red-950/30 border border-red-900/40 rounded px-2 py-1">
                {messages && messages.length === issues.length ? messages[i] : iss}
              </li>
            ))}
          </ul>
        </div>
      )}

      {checkRows.length > 0 && (
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">Check results</div>
          <div className="rounded-md border border-border overflow-hidden">
            <table className="w-full text-xs">
              <tbody>
                {checkRows.map(([k, v]) => {
                  const failed = failKeys.has(k.toUpperCase());
                  return (
                    <tr key={k} className={failed ? "bg-red-950/20" : ""}>
                      <td className="px-2 py-1 text-zinc-500" title={tagTooltip(reg, k)}>{tagLabel(reg, k)}</td>
                      <td className={`px-2 py-1 text-right ${failed ? "text-red-400" : "text-emerald-400"}`}>
                        {failed ? "FAIL" : v === null || v === "" ? "—" : "OK"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 text-xs text-zinc-400">
        <div>Sidecar cover <span className="text-zinc-200">{track.sidecar_cover ? "yes" : "no"}</span></div>
        <div>Unreadable <span className="text-zinc-200">{track.unreadable ? "yes" : "no"}</span></div>
      </div>
    </Modal>
  );
}

/** Manually set the AudioAuditor verdict for one track.
 *
 *  REAL / FAKE is stored in the file's AUDIOAUDITOR_OVERRIDE tag, and the
 *  grader applies it AFTER every derived verdict — so a forced re-audit
 *  reproduces the user's call instead of erasing it. "Auto" clears the tag and
 *  hands the track back to AudioAuditor. */
function AuditOverride({ path, current }: { path: string; current: string | null }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const { data } = useQuery({ queryKey: ["tags", path], queryFn: () => api.tags(path) });
  const stored = String((data?.tags as Record<string, string> | undefined)?.AUDIOAUDITOR_OVERRIDE ?? "")
    .trim()
    .toUpperCase();
  const set = async (v: "REAL" | "FAKE" | null) => {
    setBusy(true);
    try {
      await api.mbAssign({ [path]: { AUDIOAUDITOR_OVERRIDE: v } });
      toast(v ? `AudioAuditor forced to ${v} — it will survive forced re-audits` : "Override cleared — AudioAuditor decides again");
      qc.invalidateQueries({ queryKey: ["tags", path] });
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["album"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1">
        {(["REAL", "FAKE"] as const).map((v) => (
          <button
            key={v}
            className={`px-2 py-0.5 rounded text-[10px] border ${
              stored === v
                ? v === "REAL"
                  ? "bg-emerald-900/60 text-emerald-300 border-emerald-800"
                  : "bg-red-900/60 text-red-300 border-red-800"
                : "bg-panel text-zinc-400 border-border hover:border-accent/50"
            }`}
            disabled={busy}
            onClick={() => set(stored === v ? null : v)}
            title={stored === v ? "Click to clear the override" : `Force AudioAuditor to report ${v}`}
          >
            {v}
          </button>
        ))}
        <button
          className={`px-2 py-0.5 rounded text-[10px] border ${
            !stored ? "bg-accent on-accent border-accent" : "bg-panel text-zinc-400 border-border hover:border-accent/50"
          }`}
          disabled={busy}
          onClick={() => set(null)}
          title="Let AudioAuditor decide"
        >
          Auto
        </button>
      </div>
      <div className="text-[10px] text-zinc-500">
        {stored
          ? `Forced ${stored} — overrides AudioAuditor${current ? ` (it reports ${current})` : ""}`
          : "AudioAuditor decides. Force it if you have verified this rip yourself."}
      </div>
    </div>
  );
}

/** The file's own credit tags the fallback line reads — PERFORMER is not in
 *  the shared TrackTags type, so the panel accepts just what it shows. */
type CreditTags = { PERFORMER?: string | null; COMPOSER?: string | null };

/** Read those two tags off a track's tag record (TrackTags does not declare
 *  PERFORMER — it is not in the standard map), for the callers that mount the
 *  panel outside a track details modal. */
export function creditTagsFrom(tags: Record<string, unknown> | null | undefined): CreditTags {
  return {
    PERFORMER: tags?.PERFORMER as string | undefined,
    COMPOSER: tags?.COMPOSER as string | undefined,
  };
}

/** Role-grouped credits for one track (`path`) or a whole album (`album`),
 *  labelled with the source they came from.
 *
 *  Mounted only when its caller opens it — that is what keeps the lookup off
 *  the modal's own mount — and it never blocks: while waiting it renders one
 *  line, and a failed or empty lookup still shows the file's own credit tags
 *  rather than an empty box. */
export function CreditsPanel({ path, album, tags }: { path?: string; album?: string; tags?: CreditTags }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["credits", path ?? album ?? ""],
    queryFn: () => api.credits({ path, album }),
    enabled: !!(path || album),
    // 502 means MusicBrainz is unavailable — retrying only stalls the panel.
    retry: false,
  });
  if (isLoading)
    return (
      <div className="flex items-center gap-1.5 text-xs text-zinc-500">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Looking up credits…
      </div>
    );
  const rows = data?.rows ?? [];
  return (
    <div className="space-y-2.5">
      {data && <CreditSource source={data.source} />}
      {rows.length ? (
        <RoleGroups rows={rows} />
      ) : (
        <div className="space-y-1">
          <div className="text-xs text-zinc-500">
            No credits found{error ? ` — ${error instanceof Error ? error.message : String(error)}` : ""}.
          </div>
          <CreditTagLines tags={tags} />
        </div>
      )}
    </div>
  );
}

/** Where the rows came from: a tag fallback must never read as MB data. */
function CreditSource({ source }: { source: "musicbrainz" | "tags" }) {
  return source === "musicbrainz" ? (
    <span className="chip bg-raise border border-border text-zinc-300" title="Roles and performers from MusicBrainz artist relations">
      MusicBrainz
    </span>
  ) : (
    <span
      className="chip bg-amber-950/30 border border-amber-900/40 text-amber-300/90"
      title="MusicBrainz states no relations for this (or the file carries no MBID) — these are the file's own credit tags"
    >
      file tags
    </span>
  );
}

/** Rows grouped under their role, in the order the server sent them. */
function RoleGroups({ rows }: { rows: CreditRow[] }) {
  const byRole = new Map<string, CreditRow[]>();
  for (const r of rows) {
    const list = byRole.get(r.role);
    if (list) list.push(r);
    else byRole.set(r.role, [r]);
  }
  return (
    <div className="space-y-2.5">
      {[...byRole].map(([role, list]) => (
        <div key={role}>
          <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500 mb-0.5">{role}</div>
          <ul className="space-y-0.5">
            {list.map((r, i) => (
              <li key={`${r.artist}-${i}`} className="flex flex-wrap items-center gap-1.5 text-xs">
                {r.mbid ? (
                  <a
                    href={`https://musicbrainz.org/artist/${r.mbid}`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-zinc-200 hover:text-accent-soft underline decoration-dotted"
                    title="Open the artist on MusicBrainz"
                  >
                    {r.artist}
                  </a>
                ) : (
                  <span className="text-zinc-200">{r.artist}</span>
                )}
                {(r.attributes ?? []).map((a, j) => (
                  <span key={j} className="chip bg-raise border border-border text-zinc-400 text-[10px]">
                    {a}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/** The file's own PERFORMER / COMPOSER tags — shown when the lookup found
 *  nothing, so an empty answer still says what is on the file. */
function CreditTagLines({ tags }: { tags?: CreditTags }) {
  const rows = ([["PERFORMER", tags?.PERFORMER], ["COMPOSER", tags?.COMPOSER]] as const)
    .map(([k, v]) => [k, typeof v === "string" ? v.trim() : ""] as const)
    .filter(([, v]) => v);
  if (!rows.length) return null;
  return (
    <div className="text-xs space-y-0.5">
      {rows.map(([k, v]) => (
        <div key={k}>
          <span className="text-zinc-500">{k} </span>
          <span className="text-zinc-300 break-all">{v}</span>
        </div>
      ))}
    </div>
  );
}
