import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Bell, BellRing, ChevronDown, ChevronRight, EyeOff, Info, Library,
  ListFilter, Loader2, RefreshCw,
} from "lucide-react";
import { api } from "../api";
import type { Watch, WatchCandidate, WatchCandidates, WatchPatch, WatchPolicy } from "../api";
import { toast } from "../store";
import Modal from "./Modal";
import ReleaseChoice from "./ReleaseChoice";

/** The watch dialog — and the two watch controls both artist pages share.
 *
 *  A watch IS a set of rules, so the dialog is those rules in one place: how
 *  far back it looks (policy), which KINDS of release group count (types),
 *  which individual release groups may ever be fetched (`include` / `exclude`),
 *  how many may be queued per check, and whether the result is added to the
 *  library or only announced. Every control carries the one line that says
 *  what it will actually do, and a rejection is shown in the server's words.
 *
 *  `WatchArtistButton` sits here rather than in either page because the
 *  MusicBrainz artist page and the library artist page need the same two
 *  states — "watch this" and "watched — manage it" — and one component is the
 *  only way they can agree on what those look like. */

/** MusicBrainz's own release-group vocabulary, in its own spellings: the five
 *  primary types, then the secondary ones. The values are the lowercase names
 *  the server stores (server/artist_watch.py), so what the dialog ticks is
 *  literally what the watch holds. */
export const PRIMARY_TYPES = ["album", "ep", "single", "broadcast", "other"] as const;
export const SECONDARY_TYPES = [
  "compilation", "soundtrack", "spokenword", "interview", "audiobook", "live",
  "remix", "dj-mix", "mixtape/street", "demo", "field recording",
] as const;

/** The label of every type MusicBrainz can state — its own capitalization. */
export const TYPE_LABELS: Record<string, string> = {
  album: "Album",
  ep: "EP",
  single: "Single",
  broadcast: "Broadcast",
  other: "Other",
  compilation: "Compilation",
  soundtrack: "Soundtrack",
  spokenword: "Spokenword",
  interview: "Interview",
  audiobook: "Audiobook",
  live: "Live",
  remix: "Remix",
  "dj-mix": "DJ-mix",
  "mixtape/street": "Mixtape/Street",
  demo: "Demo",
  "field recording": "Field recording",
};

/** A new watch's starting point — the server's own defaults. */
export const DEFAULT_TYPES = ["album", "ep"];

export function typeLabel(id: string): string {
  return TYPE_LABELS[id] ?? id;
}

/** "Album, EP + Live" — the primary types first, then the secondary ones after
 *  a "+", which is the column a user scans to see what a watch will fetch. An
 *  unknown name is printed as itself rather than dropped, so a watch holding
 *  something this build does not know still says so. */
export function typeSummary(types: string[]): string {
  const set = new Set(types);
  const prim = PRIMARY_TYPES.filter((t) => set.has(t)).map(typeLabel);
  const sec = [...set].filter((t) => !(PRIMARY_TYPES as readonly string[]).includes(t)).map(typeLabel);
  return [prim.join(", "), sec.join(", ")].filter(Boolean).join(" + ") || "no types";
}

/** Does a release group of this shape fall under the ticked types? A row
 *  matches when its PRIMARY type is ticked OR any SECONDARY type is — the rule
 *  the server applies, mirrored here only for the picker's own count readout
 *  (`allowed` on each row stays the server's verdict). */
function matchesTypes(c: WatchCandidate, types: Set<string>): boolean {
  if (types.has(c.primary_type)) return true;
  return (c.secondary_types ?? []).some((s) => types.has(s));
}

/** "never" / "just now" / "3h ago" — the same wording the wish list uses, so
 *  two pages answering "when did this last run" answer it the same way. */
export function watchTimeAgo(t: number | null | undefined): string {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** "in 35m" — when the NEXT check is due. "due now" means the worker is
 *  between cycles and this watch is next in its queue. */
export function watchTimeFrom(t: number | null | undefined): string {
  if (!t) return "—";
  const s = t - Date.now() / 1000;
  if (s <= 0) return "due now";
  if (s < 60) return "in <1m";
  if (s < 3600) return `in ${Math.floor(s / 60)}m`;
  if (s < 86400) return `in ${Math.floor(s / 3600)}h`;
  return `in ${Math.floor(s / 86400)}d`;
}

/** Every watch, plus the worker's state, in ONE request (the server browses
 *  MusicBrainz per artist, so this is cheap). Polled slowly: the two artist
 *  pages need to know whether the artist in front of them is watched, and the
 *  watched page's "next check in 12m" has to keep telling the truth. */
export function useWatches() {
  return useQuery({
    queryKey: ["watches"],
    queryFn: () => api.watches(),
    refetchInterval: 60000,
    staleTime: 15000,
  });
}

/** The release groups of one artist, with the watch's rules applied by the
 *  server. A watch that does not exist yet asks by artist id instead — same
 *  browse, same answer, so the picker works before the first save — and hands
 *  over the picks on screen, so `allowed` and `reason` describe the rules the
 *  user is looking at rather than the defaults. Re-asking as the boxes are
 *  ticked is cheap: the artist's browse is cached server-side (one MusicBrainz
 *  request per artist), and `placeholderData` keeps the rows on screen while
 *  the new answer arrives instead of flashing an empty list. */
function useCandidates(
  watch: Watch | undefined,
  artistMbid: string,
  rules: { policy: WatchPolicy; release_types: string[]; include: string[]; exclude: string[] },
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["watchCandidates", watch ? `id${watch.id}` : `mbid${artistMbid}`, watch ? "" : rules],
    queryFn: () => (watch ? api.watchCandidates(watch.id) : api.watchCandidatesFor(artistMbid, rules)),
    // Only while the picker is on screen: a browse is a real request, and a
    // dialog opened to change the policy should not spend it.
    enabled,
    staleTime: 60000,
    placeholderData: keepPreviousData,
  });
}

/** One ticked/blocked row of the picker.
 *
 *  Two states per release group, because the server keeps two lists: tick a row
 *  to put it in the allow-list ("only these"), mark it Never to put it in the
 *  block list. A row in neither list is governed by the types and the policy. */
function CandidateRow({
  c, mode, onToggle, types,
}: {
  c: WatchCandidate;
  mode: "off" | "only" | "never";
  onToggle: (mode: "off" | "only" | "never") => void;
  types: Set<string>;
}) {
  const kinds = [typeLabel(c.primary_type), ...(c.secondary_types ?? []).map(typeLabel)].filter(Boolean);
  const fetched = mode !== "never" && !c.in_library && !c.queued && matchesTypes(c, types);
  // The read-out below is mounted on demand: it fetches /api/mb/release-choice
  // on mount, and a picker holds an artist's whole discography — one
  // MusicBrainz-rhyming request per row the moment the picker opens is not a
  // picker. A row the user ALLOWS opens it by itself, because that is the row
  // whose edition the watch will actually fetch.
  const [showChoice, setShowChoice] = useState(mode === "only");
  useEffect(() => {
    if (mode === "only") setShowChoice(true);
  }, [mode]);
  return (
    <li className={`flex items-start gap-2 px-2 py-1.5 rounded-md ${fetched ? "bg-white/[0.03]" : ""}`}>
      <input
        type="checkbox"
        className="mt-0.5 shrink-0 disabled:opacity-40"
        checked={mode === "only"}
        disabled={mode === "never"}
        title={mode === "never"
          ? "Marked Never — clear that first to allow it"
          : "Only fetch this release group"}
        onChange={() => onToggle(mode === "only" ? "off" : "only")}
      />
      <div className="min-w-0 flex-1">
        <div className={`text-xs truncate ${mode === "never" ? "text-zinc-600 line-through" : "text-zinc-200"}`} title={c.title}>
          {c.title}
        </div>
        <div className="flex items-center gap-1.5 flex-wrap mt-0.5">
          <span className="text-[10px] text-zinc-500">{kinds.join(" + ")}</span>
          {c.in_library && (
            <span className="chip bg-emerald-950/40 text-emerald-300/90 border border-emerald-900/50" title="Already in your library — a watch never queues it again">
              <Library className="h-3 w-3" /> In library
            </span>
          )}
          {c.queued && (
            <span className="chip bg-sky-950/50 text-sky-300 border border-sky-900/60" title="Already in the wish queue / downloading">
              Queued
            </span>
          )}
          {!c.allowed && !c.in_library && !c.queued && mode === "off" && (
            <span className="text-[10px] text-zinc-600" title={c.reason || undefined}>
              {c.reason || "not fetched"}
            </span>
          )}
        </div>
        {showChoice ? (
          <ReleaseChoice releaseGroupMbid={c.release_group_mbid} className="mt-1" />
        ) : (
          <button
            className="mt-0.5 text-[10px] text-zinc-500 hover:text-zinc-300 tap"
            onClick={() => setShowChoice(true)}
            title="Which edition the download policy would fetch for this release group"
          >
            Which edition?
          </button>
        )}
      </div>
      <button
        className={`shrink-0 p-1 rounded-md ${mode === "never" ? "text-red-300 bg-red-950/50" : "text-zinc-600 hover:text-red-300 hover:bg-white/5"}`}
        title={mode === "never" ? "Blocked — click to unblock" : "Never fetch this release group"}
        aria-label="Never fetch this release group"
        onClick={() => onToggle(mode === "never" ? "off" : "never")}
      >
        <EyeOff className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}

/** The release-group picker: the artist's release groups grouped by year, each
 *  row ticked (only these) or marked Never, with the bulk helpers that turn a
 *  discography into a short list in one click. */
function ReleasePicker({
  candidates, types, include, exclude, onSet,
}: {
  candidates: WatchCandidates;
  types: Set<string>;
  include: Set<string>;
  exclude: Set<string>;
  onSet: (include: Set<string>, exclude: Set<string>) => void;
}) {
  const rows = useMemo(() => {
    const byYear = new Map<string, WatchCandidate[]>();
    for (const c of candidates.items ?? []) {
      const key = c.year || "Undated";
      const list = byYear.get(key);
      if (list) list.push(c);
      else byYear.set(key, [c]);
    }
    // Newest first, undated last: a watch is about what is out now.
    return [...byYear.entries()].sort((a, b) => {
      if (a[0] === "Undated") return 1;
      if (b[0] === "Undated") return -1;
      return b[0].localeCompare(a[0]);
    });
  }, [candidates.items]);

  const modeOf = (c: WatchCandidate): "off" | "only" | "never" =>
    include.has(c.release_group_mbid) ? "only" : exclude.has(c.release_group_mbid) ? "never" : "off";

  const toggle = (c: WatchCandidate, mode: "off" | "only" | "never") => {
    const next = new Set(include);
    const drop = new Set(exclude);
    next.delete(c.release_group_mbid);
    drop.delete(c.release_group_mbid);
    if (mode === "only") next.add(c.release_group_mbid);
    if (mode === "never") drop.add(c.release_group_mbid);
    onSet(next, drop);
  };

  /** A bulk helper only ever lists release groups the watch could fetch:
   *  something already in the library or already queued is never in a list,
   *  because listing it would say the watch is about to fetch it. */
  const setOnly = (pred: (c: WatchCandidate) => boolean) =>
    onSet(new Set(candidates.items.filter((c) => pred(c) && !c.in_library && !c.queued)
      .map((c) => c.release_group_mbid)), new Set());

  // What this watch would actually fetch right now: allowed by the ticked
  // types, not already held, not blocked. Rows in the library or already
  // queued are counted separately rather than as pending work.
  const fetchNow = candidates.items.filter(
    (c) => !c.in_library && !c.queued && !exclude.has(c.release_group_mbid)
      && matchesTypes(c, types) && (include.size === 0 || include.has(c.release_group_mbid)),
  );
  const held = candidates.items.filter((c) => c.in_library || c.queued).length;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1.5 flex-wrap">
        <button className="btn-ghost !py-1 text-[11px]" onClick={() => setOnly((c) => c.primary_type === "album" && c.is_new)}
          title="Allow only the albums the watch has not seen — replaces both lists, so a block set earlier is lifted too">
          Only new albums
        </button>
        <button className="btn-ghost !py-1 text-[11px]" onClick={() => setOnly((c) => c.primary_type === "album")}
          title="Allow every album release group of this artist, new or old — replaces both lists, so a block set earlier is lifted too">
          Albums only
        </button>
        <button className="btn-ghost !py-1 text-[11px]" onClick={() => onSet(new Set(), new Set())}
          title="Clear both lists — selects none, and the watch goes back to any new release of the allowed types">
          Nothing (new releases only)
        </button>
      </div>

      <div className="text-[11px] text-zinc-400 leading-snug">
        {include.size === 0 && exclude.size === 0 ? (
          <>
            Nothing is selected: the watch takes <span className="text-zinc-200">any new release of the allowed types</span>.
            Tick a row to allow it and nothing else; mark a row Never to block it for good.
          </>
        ) : (
          <>
            {include.size > 0 && (
              <>Only the {include.size} ticked release{include.size === 1 ? "" : "s"} — nothing else is ever fetched. </>
            )}
            {exclude.size > 0 && (
              <>{exclude.size} release{exclude.size === 1 ? "" : "s"} blocked: never fetched, even by a backfill.</>
            )}
          </>
        )}
      </div>

      <div className="text-[11px] text-zinc-500">
        Would fetch {fetchNow.length} release group{fetchNow.length === 1 ? "" : "s"} now
        {held > 0 ? ` · ${held} already in the library or queued (not counted)` : ""}
        {candidates.notes?.policy ? ` · ${candidates.notes.policy}` : ""}
      </div>

      <div className="max-h-72 overflow-y-auto rounded-lg border border-border bg-panel/40 p-1">
        {rows.length === 0 && (
          <div className="text-[11px] text-zinc-500 px-2 py-2">
            MusicBrainz lists no release groups for this artist, so there is nothing to pick — the watch has
            nothing to fetch until one is added there.
          </div>
        )}
        {rows.map(([year, list]) => (
          <div key={year} className="mb-1">
            <div className="text-[10px] uppercase tracking-widest text-zinc-500 px-2 pt-1.5">{year} · {list.length}</div>
            <ul>
              {list.map((c) => (
                <CandidateRow key={c.release_group_mbid} c={c} mode={modeOf(c)} types={types}
                  onToggle={(m) => toggle(c, m)} />
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

/** The dialog itself. `watch` is undefined while creating one; `focusReleases`
 *  opens it with the picker expanded, which is what the artist pages'
 *  "Choose releases…" needs. */
export default function WatchDialog({
  artistMbid, artist, watch, focusReleases = false, onClose,
}: {
  artistMbid: string;
  artist: string;
  watch?: Watch;
  focusReleases?: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [policy, setPolicy] = useState<WatchPolicy>(watch?.policy ?? "new_only");
  const [types, setTypes] = useState<Set<string>>(new Set(watch?.release_types ?? DEFAULT_TYPES));
  const [maxPerCycle, setMaxPerCycle] = useState(watch?.max_per_cycle ?? 1);
  const [autoAdd, setAutoAdd] = useState(watch?.auto_add ?? true);
  const [include, setInclude] = useState<Set<string>>(new Set(watch?.include ?? []));
  const [exclude, setExclude] = useState<Set<string>>(new Set(watch?.exclude ?? []));
  const [showReleases, setShowReleases] = useState(focusReleases);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const candidates = useCandidates(
    watch,
    artistMbid,
    { policy, release_types: [...types], include: [...include], exclude: [...exclude] },
    showReleases,
  );

  const toggleType = (id: string) =>
    setTypes((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const save = async () => {
    if (!types.size) {
      setError("Pick at least one release type — a watch with no type would never have anything to fetch.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const patch: WatchPatch = {
        policy,
        release_types: [...types],
        max_per_cycle: maxPerCycle,
        auto_add: autoAdd,
        include: [...include],
        exclude: [...exclude],
      };
      if (watch) await api.watchUpdate(watch.id, patch);
      else await api.watchAdd({ ...patch, artist_mbid: artistMbid, artist });
      toast.success(watch ? `Watch updated (${artist})` : `Watching ${artist} for new releases`);
      // The list AND the picker: the saved rules decide which rows the server
      // would fetch, and both are read from the server's answer.
      qc.invalidateQueries({ queryKey: ["watches"] });
      qc.invalidateQueries({ queryKey: ["watchCandidates"] });
      onClose();
    } catch (e) {
      // The server's own words (400 unknown artist, 409 already watched, 400
      // type not in the vocabulary) — verbatim, not paraphrased.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      onClose={onClose}
      title={watch ? `Watch — ${artist || "this artist"}` : `Watch ${artist || "this artist"} for new releases`}
      subtitle="A watch queues a few release groups per check into the wish queue, then the normal search-and-import runs. It never queues a discography."
      bodyClass="px-5 py-4 space-y-5"
      footer={
        <div className="flex items-start gap-2 flex-wrap">
          {error && (
            <div className="text-[11px] text-red-300 flex-1 min-w-[50%] leading-snug" role="alert">{error}</div>
          )}
          {!error && (
            <div className="text-[10px] text-zinc-600 flex-1 leading-snug">
              Checks run on the watcher's own schedule; the watched-artists page shows when the next one is due.
            </div>
          )}
          <button className="btn-ghost !py-1.5 text-xs" onClick={onClose}>Cancel</button>
          <button className="btn-primary !py-1.5 text-xs" onClick={save} disabled={busy}>
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {watch ? "Save watch" : "Start watching"}
          </button>
        </div>
      }
    >
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 pb-1.5">When it looks</div>
        <div className="space-y-1.5" role="radiogroup" aria-label="Watch policy">
          {([
            ["new_only", "New releases only", "Nothing that was already out when you started watching is fetched — only release groups the watch has not seen."],
            ["backfill", "Backfill what is missing", "Deliberately walks what is already out, max per cycle at a time, so an old discography fills in gradually."],
          ] as const).map(([id, label, why]) => (
            <label key={id} className={`flex items-start gap-2.5 rounded-lg border px-3 py-2 cursor-pointer transition-colors ${
              policy === id ? "border-accent/50 bg-white/[0.04]" : "border-border bg-panel/40 hover:bg-white/[0.02]"
            }`}>
              <input
                type="radio"
                name="watch-policy"
                className="mt-1 shrink-0"
                style={{ accentColor: "rgb(var(--accent))" }}
                checked={policy === id}
                onChange={() => setPolicy(id)}
              />
              <span className="min-w-0">
                <span className="text-xs font-medium text-zinc-200">{label}{id === "new_only" ? " (default)" : ""}</span>
                <span className="block text-[11px] text-zinc-500 leading-snug">{why}</span>
              </span>
            </label>
          ))}
        </div>
      </div>

      <div>
        <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 pb-1.5">Which release types</div>
        <div className="space-y-2">
          {([["Primary", PRIMARY_TYPES], ["Secondary", SECONDARY_TYPES]] as const).map(([group, ids]) => (
            <div key={group}>
              <div className="text-[10px] uppercase tracking-wider text-zinc-600 pb-1">{group}</div>
              <div className="flex flex-wrap gap-1.5">
                {ids.map((id) => (
                  <button
                    key={id}
                    className={`chip border tap ${types.has(id)
                      ? "bg-accent/15 border-accent/40 text-accent-soft"
                      : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
                    aria-pressed={types.has(id)}
                    title={`${typeLabel(id)} — MusicBrainz's own type name: ${id}`}
                    onClick={() => toggleType(id)}
                  >
                    {typeLabel(id)}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="text-[11px] text-zinc-500 pt-2 leading-snug">
          The watcher only downloads release groups of these types — a row counts when its primary type is ticked
          or any of its secondary types is.
        </div>
      </div>

      <div className="section !border-border/40 !pt-4">
        <button className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-400 hover:text-white"
          onClick={() => setShowReleases((v) => !v)} aria-expanded={showReleases}>
          {showReleases ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          Which releases
          <span className="text-[10px] normal-case tracking-normal text-zinc-600">
            {include.size || exclude.size ? `${include.size} only · ${exclude.size} never` : "any new release"}
          </span>
        </button>
        {showReleases && (
          <div className="pt-2.5">
            {candidates.isLoading ? (
              <div className="text-xs text-zinc-500 flex items-center gap-2">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Asking MusicBrainz for this artist's release groups…
              </div>
            ) : candidates.error ? (
              <div className="text-[11px] text-amber-300/90 flex items-start gap-2">
                <Info className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                <span className="flex-1 leading-snug">
                  {candidates.error instanceof Error ? candidates.error.message : String(candidates.error)}
                </span>
                <button className="btn-ghost !py-1 text-[11px] shrink-0" onClick={() => void candidates.refetch()}>
                  <RefreshCw className="h-3 w-3" /> Retry
                </button>
              </div>
            ) : candidates.data ? (
              <ReleasePicker
                candidates={candidates.data}
                types={types}
                include={include}
                exclude={exclude}
                onSet={(inc, exc) => { setInclude(inc); setExclude(exc); }}
              />
            ) : null}
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500" htmlFor="watch-max">
            Max per cycle
          </label>
          <select
            id="watch-max"
            className="input !py-1.5 text-xs mt-1"
            value={maxPerCycle}
            onChange={(e) => setMaxPerCycle(Number(e.target.value))}
          >
            {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
          <div className="text-[11px] text-zinc-500 pt-1 leading-snug">
            The ceiling per check: one cycle queues at most this many release groups. A discography takes as many
            cycles as it has releases.
          </div>
        </div>
        <div>
          <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">What to do with them</span>
          <label className="flex items-start gap-2.5 rounded-lg border border-border bg-panel/40 px-3 py-2 mt-1 cursor-pointer">
            <input
              type="checkbox"
              className="mt-0.5 shrink-0"
              checked={autoAdd}
              onChange={(e) => setAutoAdd(e.target.checked)}
            />
            <span className="min-w-0">
              <span className="text-xs text-zinc-200">Add to the library automatically</span>
              <span className="block text-[11px] text-zinc-500 leading-snug">
                {autoAdd
                  ? "On: the release is queued straight into the library and the usual search-and-import runs."
                  : "Off: you are told about it and it sits in the wish list until you ask for it."}
              </span>
            </span>
          </label>
        </div>
      </div>

      {(include.size > 0 || exclude.size > 0) && (
        <div className="text-[10px] text-zinc-600 leading-snug">
          Saved with the watch: {include.size} release group{include.size === 1 ? "" : "s"} in the allow-list,{" "}
          {exclude.size} blocked. Nothing outside the allow-list is fetched while it is non-empty.
        </div>
      )}
    </Modal>
  );
}

/** The Watch control the two artist pages share.
 *
 *  Unwatched: one button that opens the dialog. Watched: the state and the way
 *  to manage it (the watched-artists page), plus the release picker itself —
 *  the flow the artist page needs to answer "which of these should be
 *  fetched?" without leaving the artist. An artist with no MusicBrainz id
 *  cannot be watched at all, and the disabled button says exactly that rather
 *  than vanishing. */
export function WatchArtistButton({
  artistMbid, artist, buttonClass = "btn-ghost !py-1.5 text-xs",
}: {
  artistMbid: string;
  artist: string;
  buttonClass?: string;
}) {
  const { data } = useWatches();
  const [open, setOpen] = useState(false);
  const [picker, setPicker] = useState(false);
  const watch = artistMbid ? data?.watches.find((w) => w.artist_mbid === artistMbid) : undefined;

  if (!artistMbid) {
    return (
      <button className={buttonClass} disabled title="This artist has no MusicBrainz id, so there is no release list to watch">
        <Bell className="h-3.5 w-3.5" /> Watch for new releases
      </button>
    );
  }

  return (
    <>
      {watch ? (
        <>
          <Link
            className={buttonClass}
            to="/watched"
            title={`Watching for new releases — ${watch.enabled ? "checking" : "paused"}${watch.last_result ? ` · last check: ${watch.last_result}` : ""}. Manage every watch here.`}
          >
            <BellRing className="h-3.5 w-3.5" /> {watch.enabled ? "Watching — manage" : "Paused — manage"}
          </Link>
          <button
            className={buttonClass}
            onClick={() => { setPicker(true); setOpen(true); }}
            title="Pick which of this artist's release groups the watch may fetch"
          >
            <ListFilter className="h-3.5 w-3.5" /> Choose releases…
          </button>
        </>
      ) : (
        <button
          className={buttonClass}
          onClick={() => { setPicker(false); setOpen(true); }}
          title="Get a few new releases of this artist as they come out — never the whole discography"
        >
          <Bell className="h-3.5 w-3.5" /> Watch for new releases
        </button>
      )}
      {open && (
        <WatchDialog
          artistMbid={artistMbid}
          artist={artist}
          watch={watch}
          focusReleases={picker}
          onClose={() => { setOpen(false); setPicker(false); }}
        />
      )}
    </>
  );
}
