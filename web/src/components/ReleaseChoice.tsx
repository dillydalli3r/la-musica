import { useState } from "react";
import type { ReactNode } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Disc3 } from "lucide-react";
import { api } from "../api";
import type { MBReleaseChoiceEdition, MBReleaseChoicePolicy } from "../types";

/** The server ranks at most this many editions; the list draws no more than
 *  it received, and never more than this. */
const MAX_ROWS = 20;

/** "1997-05-21 · FR · CD · 16 tracks · Official" — one edition, in the order
 *  the fields are read. A value MusicBrainz does not state is left out rather
 *  than printed as "unknown": an edition without a country should not read
 *  like one released nowhere. */
function releaseLine(e: MBReleaseChoiceEdition): string {
  const media = (e.media ?? []).filter(Boolean).join(" + ");
  const parts = [
    e.date || "",
    e.country || "",
    media,
    e.track_count ? `${e.track_count} track${e.track_count === 1 ? "" : "s"}` : "",
    e.status || "",
  ].filter(Boolean);
  return parts.join(" · ") || "no edition details";
}

/** The chip short form of one policy reason: the fact alone, without the
 *  parenthetical rank or the trailing clause — "CD — preferred medium (order
 *  1)" is a chip, "(order 1)" is the tooltip's job. Anything the patterns do
 *  not recognise keeps the engine's own words, so a reason added later still
 *  reads correctly. */
function chipLabel(reason: string): string {
  return reason
    .replace(/\s*\([^)]*\)\s*/g, " ")
    .trim()
    .replace(/^(\d+)\/(\d+) tracks?\b.*$/i, "$1/$2 tracks")
    .replace(/^(\d+) of (\d+) tracks?\b.*$/i, "$1/$2 tracks")
    .replace(/^(official|promotion|bootleg)\s+release$/i, "$1")
    .replace(/^original release date\b.*$/i, "original edition");
}

/** The policy RULE behind a reason, for the chip's tooltip: the reason states
 *  a fact about one edition ("16/16 tracks of the release group"), the rule
 *  states what that fact means ("a release short of the release group's track
 *  count is penalised"), and the two are paired by the words they share. No
 *  match is fine — the chip falls back to the reason's own sentence, which is
 *  why a reworded rule can never make the UI lie. */
const RULE_HINTS: [RegExp, RegExp][] = [
  [/official|promotion|bootleg|unofficial/i, /official|promotion|bootleg/i],
  [/\bcd\b|medium|digital|vinyl/i, /medium order|physical|digital/i],
  [/tracks? of the release group|complete|short of/i, /track count|complete/i],
  [/original release date|reissue|deluxe|later edition/i, /reissue|original edition/i],
  [/country/i, /country/i],
  [/clean|edited|explicit audio/i, /explicit|clean|altered/i],
  [/disambig|parenthes/i, /plain|parenthes/i],
];

function ruleFor(reason: string, rules: string[]): string {
  for (const [reasonHint, ruleHint] of RULE_HINTS) {
    if (!reasonHint.test(reason)) continue;
    const rule = rules.find((r) => ruleHint.test(r));
    if (rule) return rule;
  }
  return "";
}

/** The configured policy in one muted line — the knobs, not the prose: the
 *  prose is each chip's tooltip, and this is what a user checks after a visit
 *  to Settings. */
function policyLine(p: MBReleaseChoicePolicy): string {
  const parts = [
    p.medium_order?.length ? p.medium_order.join(" → ") : "any medium",
    p.status_order?.length ? p.status_order.join(" → ") : "",
    p.preferred_country ? `released in ${p.preferred_country} preferred` : "any country",
    p.prefer_original_edition ? "original edition preferred" : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

/** Which edition the download policy will fetch for one release group, why,
 *  and every alternative it ranked — the user can force one of those and the
 *  server re-ranks with it.
 *
 *  The component decides nothing itself: it renders what
 *  `/api/mb/release-choice` answered (the same policy the auto-import and the
 *  watch run) and hands a forced release id up through `onOverride`. The
 *  caller owns `override` because the add call needs the same id — passing it
 *  back to the server as `prefer` is what makes the server explain the forced
 *  pick in its own words instead of the UI explaining it. Without
 *  `onOverride` it is a read-only statement of intent (the watch dialog's
 *  candidate rows use it that way). */
export default function ReleaseChoice({
  releaseGroupMbid,
  primaryType = "",
  secondaryType = "",
  override = "",
  onOverride,
  className = "",
}: {
  releaseGroupMbid: string;
  /** The release-group kind the caller is after, in the vocabulary
   *  mlo/naming exposes (PRIMARY_RELEASE_TYPES / SECONDARY_RELEASE_TYPES) —
   *  the same values /api/mb/search takes. Empty = whatever kind the group
   *  itself is, which is what the server then echoes back in `release_group`. */
  primaryType?: string;
  secondaryType?: string;
  /** The release the user forced; "" means "the policy's own pick". */
  override?: string;
  onOverride?: (releaseMbid: string) => void;
  className?: string;
}) {
  // null = the user has not said. The list opens itself whenever there is
  // nothing the app will take unattended — no pick at all, or a pick the
  // eligibility rules refuse — because choosing one is then the user's job.
  // A real, fetchable pick keeps it collapsed so the panel stays one line.
  const [open, setOpen] = useState<boolean | null>(null);
  const q = useQuery({
    queryKey: ["mbReleaseChoice", releaseGroupMbid, primaryType, secondaryType, override],
    queryFn: () =>
      api.releaseChoice({
        releaseGroupMbid,
        prefer: override || undefined,
        primaryType,
        secondaryType,
      }),
    enabled: !!releaseGroupMbid,
    // Forcing another edition must not blank the panel while the re-ranked
    // answer is on its way.
    placeholderData: keepPreviousData,
    // The policy only moves when the config or the group does, and MusicBrainz
    // is rate-limited: one answer is worth keeping for the visit.
    staleTime: 5 * 60 * 1000,
  });

  const wrap = (children: ReactNode) => (
    <div className={`rounded-lg border border-border bg-panel/40 px-3 py-2 ${className}`}>{children}</div>
  );

  if (!releaseGroupMbid) return null;
  if (q.isLoading) return wrap(<span className="text-[11px] text-zinc-500">Asking which edition to fetch…</span>);
  if (q.isError) {
    const msg = q.error instanceof Error ? q.error.message : String(q.error);
    return wrap(
      <span className="text-[11px] text-amber-300/80" title={msg}>
        The release choice could not be read ({msg}) — adding this release group still uses the same policy.
      </span>
    );
  }
  const data = q.data;
  if (!data) return null;

  const chosen = data.chosen;
  const rows = (data.candidates ?? []).slice(0, MAX_ROWS);
  const hidden = (data.candidates?.length ?? 0) - rows.length;
  const rules = data.policy?.rules ?? [];
  const expanded = open ?? !(chosen && chosen.eligible !== false);

  const list = rows.length ? (
    <div className="mt-1.5">
      <button
        className="inline-flex items-center gap-1 text-[10px] text-zinc-500 hover:text-zinc-300 tap"
        onClick={() => setOpen(!expanded)}
        aria-expanded={expanded}
        title="Every edition the policy ranked, best first"
      >
        {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        {rows.length} edition{rows.length === 1 ? "" : "s"} ranked
        {hidden > 0 ? ` (+${hidden} more)` : ""}
      </button>
      {expanded && (
        <div className="mt-1 rounded-md border border-border/70 divide-y divide-white/5">
          <ul className="stagger">
            {rows.map((r) => {
              const isPick = chosen ? r.release_mbid === chosen.release_mbid : false;
              // A payload is an answer from the server, not a promise: a field
              // it does not state must cost that row a blank, never the panel.
              const score = r.score ?? 0;
              const reasons = r.reasons ?? [];
              return (
                <li key={r.release_mbid} className="flex items-start gap-2 px-2 py-1.5">
                  <span
                    className="w-8 shrink-0 pt-0.5 text-[11px] tabular-nums text-zinc-500"
                    title={`Policy score ${score.toFixed(3)} — higher is better`}
                  >
                    {Math.round(score * 100)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-xs text-zinc-300" title={r.title}>
                      {r.title}
                      {r.disambiguation ? <span className="text-zinc-500"> ({r.disambiguation})</span> : null}
                    </span>
                    <span className="block truncate text-[11px] text-zinc-500">{releaseLine(r)}</span>
                    <span className="block truncate text-[10px] text-zinc-600" title={reasons.join("; ")}>
                      {reasons.join(" · ")}
                    </span>
                  </span>
                  <span className="shrink-0 pt-0.5">
                    {isPick && (
                      <span
                        className="chip border border-accent/40 bg-accent/10 text-accent-soft"
                        title={override
                          ? "The edition you forced"
                          : r.eligible === false
                            ? "The policy's best edition, but an unattended download refuses it"
                            : "What will be fetched"}
                      >
                        {override ? "your pick" : "policy pick"}
                      </span>
                    )}
                    {r.eligible === false && (
                      // A hard verdict, not a warning: unattended downloads skip
                      // this edition, and the leading reason names the setting
                      // that skips it.
                      <span
                        className="chip border border-amber-800/60 bg-amber-950/40 text-amber-300/90"
                        title={reasons[0] || "Auto-import will not download this edition on its own"}
                      >
                        not eligible
                      </span>
                    )}
                    {onOverride && !isPick && (
                      <button
                        className="btn-ghost !px-2 !py-0.5 text-[11px]"
                        onClick={() => onOverride(r.release_mbid)}
                        title={`Fetch this edition instead — ${releaseLine(r)}`}
                      >
                        Use this
                      </button>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
          {data.policy && (
            <div className="px-2 py-1 text-[10px] text-zinc-600 truncate" title={rules.join("\n")}>
              Policy: {policyLine(data.policy)}
            </div>
          )}
        </div>
      )}
    </div>
  ) : null;

  if (!chosen) {
    // `chosen` is null in exactly two states (the engine's own rule): the group
    // has no editions at all, or none of them is the KIND the caller asked for.
    // The kind named is the one the CALLER passed, never the echoed
    // release_group type — the echo is the group's own type, which is a
    // different thing the moment a filter was asked for.
    const asked = [primaryType, secondaryType].filter(Boolean).join(" + ");
    const askedLabel = asked ? asked[0].toUpperCase() + asked.slice(1) : "";
    return wrap(
      <>
        <div className="text-[11px] text-amber-300/80">
          {rows.length
            ? `No ${askedLabel ? `${askedLabel} edition` : "edition of the kind you asked for"} in this release group — ${rows.length} other edition${rows.length === 1 ? "" : "s"} ranked.`
            : "MusicBrainz knows no edition of this release group."}
        </div>
        {list}
      </>
    );
  }

  // eligible === false is a hard verdict, not a hint: an unattended download
  // (the watch, the bulk import) will not take this edition. Forcing one with
  // an override is a different route — the add call uses the id as given — so
  // the panel must not promise this edition until the user has forced it.
  const refused = chosen.eligible === false;
  return wrap(
    <>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-zinc-500">
          <Disc3 className="h-3 w-3" />{" "}
          {override ? "Will fetch (your pick)" : refused ? "Best edition — auto-import refuses it" : "Will fetch"}
        </span>
        {override && onOverride && (
          <button
            className="text-[10px] text-accent-soft hover:underline tap"
            onClick={() => onOverride("")}
            title="Drop the override and let the policy choose again"
          >
            use the policy&rsquo;s pick
          </button>
        )}
      </div>
      <div className="mt-0.5 flex items-baseline gap-2 flex-wrap">
        <span className="text-xs font-medium text-zinc-100 truncate" title={chosen.title}>
          {chosen.title}
        </span>
        <span className="text-[11px] text-zinc-500">{releaseLine(chosen)}</span>
      </div>
      {chosen.reasons?.length ? (
        <div className="mt-1 flex flex-wrap gap-1">
          {chosen.reasons.map((reason) => {
            const rule = ruleFor(reason, rules);
            return (
              <span
                key={reason}
                className="chip border border-border bg-raise text-zinc-300"
                title={rule ? `${reason}\nPolicy: ${rule}` : reason}
              >
                {chipLabel(reason)}
              </span>
            );
          })}
        </div>
      ) : null}
      {refused && !override && (
        // The leading reason IS the rule that refuses it, so it is quoted here
        // rather than described: the user is told which setting to look at.
        <div className="mt-1 text-[11px] text-amber-300/80">
          An unattended download will not take this edition
          {chosen.reasons?.[0] ? ` — ${chosen.reasons[0]}` : ""}. Pick one below to fetch it anyway.
        </div>
      )}
      {list}
    </>
  );
}
