import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, CalendarClock, Check, ChevronDown, ChevronRight, Clock,
  Database, Loader2, Music4, RefreshCw, Tags as TagsIcon,
} from "lucide-react";
import { api } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import { EmptyState, PageLoading } from "../components/Badges";
import { toast } from "../store";
import type { TagAlbum, TagAlbumDetail, TagJob } from "../types";

/** MAINTAIN → Tags: MusicBrainz freshness of the files themselves.
 *
 *  The date is not a table: every MusicBrainz writer stamps
 *  `MUSICBRAINZ_UPDATED` on the track, so an album's age is its OLDEST
 *  track's stamp and a refresh updates the WHOLE album. This page reads that
 *  through `/api/tags/status` (library, per artist, per album), expands one
 *  album's per-track stamps on demand, and owns the refresh queue's controls.
 *  Scheduling itself is configuration — the same keys Settings → Tags edits —
 *  written here through the one config route. */

const iso = (s: string | null | undefined) => {
  if (!s) return "never";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
};
const day = (s: string | null | undefined) => {
  if (!s) return "never pulled";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleDateString();
};

export default function TagsPage() {
  const qc = useQueryClient();
  const [days, setDays] = useState<number | null>(null);
  const [filter, setFilter] = useState("");
  const [view, setView] = useState<"albums" | "artists">("albums");
  const [openArtist, setOpenArtist] = useState<string | null>(null);
  const [openAlbum, setOpenAlbum] = useState<string | null>(null);

  const status = useQuery({
    queryKey: ["tagsStatus", days],
    queryFn: () => api.tagsStatus(days ?? undefined),
    // While the queue runs, the page follows it; idle it only needs a
    // fresh read when the user asks (Refresh) or a job finishes.
    refetchInterval: (q) => (q.state.data?.job?.status === "running" ? 2000 : false),
  });
  const cfg = useQuery({ queryKey: ["config"], queryFn: api.config });
  const data = status.data;

  const save = async (patch: Record<string, unknown>) => {
    try {
      await api.saveConfig({ ...(cfg.data ?? {}), ...patch });
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["tagsStatus"] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const startRefresh = async (paths: string[] = [], due = true) => {
    try {
      const r = await api.tagsRefresh(paths, due);
      if (!r.started) toast(r.reason || "Nothing to refresh");
      else toast(`Refreshing ${r.job?.total ?? 0} album(s) — MusicBrainz is asked once per album`);
      qc.invalidateQueries({ queryKey: ["tagsStatus"] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const q = filter.trim().toLowerCase();
  const albums = useMemo(
    () => (data?.albums ?? []).filter(
      (a) => !q || a.artist.toLowerCase().includes(q) || a.album.toLowerCase().includes(q)
    ),
    [data, q]
  );
  const grouped = useMemo(() => {
    const out = new Map<string, TagAlbum[]>();
    for (const a of albums) {
      const list = out.get(a.artist) ?? [];
      list.push(a);
      out.set(a.artist, list);
    }
    return [...out.entries()];
  }, [albums]);

  if (status.isLoading || !data) return <PageLoading label="Reading the library's MusicBrainz stamps…" />;

  const counts = data.counts;
  const job: TagJob | null = data.job;
  const running = job?.status === "running";
  const pct = job && job.total ? Math.round((job.done / job.total) * 100) : 0;

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1500px]">
      <PageHeader
        icon={TagsIcon}
        overline="Maintain"
        title="Tags"
        subtitle="How fresh each album's MusicBrainz data is — read from the files' own MUSICBRAINZ_UPDATED stamps, never from a database. An album is as old as its OLDEST track, and a refresh updates the whole album."
        actions={
          <>
            <button
              className="btn-primary tap"
              disabled={running || !counts.due}
              onClick={() => void startRefresh([], true)}
              title="Refresh every album past the age below, one at a time per worker"
            >
              {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {running ? "Refreshing…" : `Refresh due (${counts.due})`}
            </button>
            <button
              className="btn-ghost tap"
              disabled={running || !counts.albums}
              onClick={() => void startRefresh([], false)}
              title="Refresh every album that names a MusicBrainz release, due or not"
            >
              <Database className="h-4 w-4" /> All linked
            </button>
            <button
              className="btn-ghost tap"
              onClick={() => {
                qc.invalidateQueries({ queryKey: ["tagsStatus"] });
                void status.refetch();
              }}
              title="Re-read the files' stamps now"
            >
              <RefreshCw className="h-4 w-4" /> Re-read
            </button>
          </>
        }
      >
        <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-400">
          <label className="flex items-center gap-2">
            Due when older than
            <input
              className="input w-20 py-1 tap"
              type="number"
              min={1}
              max={3650}
              value={days ?? data.days}
              onChange={(e) => setDays(Number(e.target.value) || null)}
            />
            days
          </label>
          <span className="text-zinc-600">·</span>
          <label className="flex items-center gap-2" title="Scheduled refreshes run in the background; the queue is bounded and never updates the whole library at once">
            <input
              type="checkbox"
              checked={!!(cfg.data?.tag_refresh_enabled ?? data.scheduled)}
              onChange={(e) => void save({ tag_refresh_enabled: e.target.checked })}
            />
            Refresh on a schedule
          </label>
          <label className="flex items-center gap-2">
            every
            <input
              className="input w-16 py-1 tap"
              type="number"
              min={1}
              max={3650}
              value={Number(cfg.data?.tag_refresh_days ?? data.days)}
              onChange={(e) => void save({ tag_refresh_days: Math.max(1, Number(e.target.value) || 7) })}
            />
            days
          </label>
          <label className="flex items-center gap-2" title="Albums refreshed in parallel; MusicBrainz itself is one request a second">
            workers
            <input
              className="input w-14 py-1 tap"
              type="number"
              min={1}
              max={16}
              value={Number(cfg.data?.tag_refresh_workers ?? 4)}
              onChange={(e) => void save({ tag_refresh_workers: Math.max(1, Math.min(16, Number(e.target.value) || 4)) })}
            />
          </label>
          <label className="flex items-center gap-2" title="How often the background scheduler looks for due albums (minutes)">
            check every
            <input
              className="input w-16 py-1 tap"
              type="number"
              min={5}
              max={1440}
              value={Number(cfg.data?.tag_refresh_interval_minutes ?? 60)}
              onChange={(e) => void save({ tag_refresh_interval_minutes: Math.max(5, Math.min(1440, Number(e.target.value) || 60)) })}
            />
            min
          </label>
        </div>
      </PageHeader>

      {/* Summary */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card icon={Database} label="Albums tracked" value={counts.albums.toLocaleString()}
          hint={`${counts.tracks.toLocaleString()} tracks · ${counts.unlinked} album(s) with no MusicBrainz link`} />
        <Card icon={CalendarClock} label="Due" value={counts.due.toLocaleString()}
          hint={`past ${data.days} days or never pulled`} tone={counts.due ? "warn" : "ok"} />
        <Card icon={Clock} label="Oldest pull" value={day(data.oldest)}
          hint={data.oldest ? iso(data.oldest) : "no album carries a stamp yet"} />
        <Card icon={RefreshCw} label="Schedule" value={data.scheduled ? `every ${cfg.data?.tag_refresh_days ?? data.days} d` : "off"}
          hint={data.scheduled ? `checks every ${data.interval_minutes} min · last auto ${data.last_auto ? iso(new Date(data.last_auto * 1000).toISOString()) : "—"}` : "turn it on above; nothing runs by itself"} />
      </div>

      {/* Queue */}
      {job && (
        <div className={`panel p-3 space-y-2 ${job.status === "error" ? "border-red-900/60" : ""}`}>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {running ? <Loader2 className="h-4 w-4 animate-spin text-accent" />
              : job.status === "error" ? <AlertTriangle className="h-4 w-4 text-red-400" />
              : <Check className="h-4 w-4 text-emerald-500" />}
            <span className="font-medium">
              {running ? "Refreshing MusicBrainz tags" : job.status === "error" ? "Refresh failed" : "Refresh finished"}
            </span>
            <span className="text-zinc-500 text-xs">
              {job.done}/{job.total} albums · {job.refreshed} refreshed · {job.written} file(s) written
              {job.skipped.length ? ` · ${job.skipped.length} skipped` : ""}
              {job.errors.length ? ` · ${job.errors.length} error(s)` : ""}
              {job.workers ? ` · ${job.workers} worker(s)` : ""}
            </span>
            {running && job.current && (
              <span className="text-zinc-500 text-xs ml-auto truncate max-w-[45%]" title={job.current}>
                {job.current.split(/[\\/]/).filter(Boolean).slice(-1)[0]}
              </span>
            )}
          </div>
          <div className="h-1.5 rounded bg-raise overflow-hidden">
            <div className="h-full bg-accent transition-[width]" style={{ width: `${pct}%` }} />
          </div>
          {!!job.skipped.length && (
            <details className="text-xs text-zinc-500">
              <summary className="cursor-pointer">{job.skipped.length} album(s) skipped — why</summary>
              <ul className="mt-1 space-y-0.5">
                {job.skipped.slice(0, 30).map((s) => (
                  <li key={s.path} className="truncate" title={`${s.path} — ${s.reason}`}>
                    {s.path.split(/[\\/]/).filter(Boolean).slice(-1)[0]} — {s.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {!!job.errors.length && (
            <details className="text-xs text-red-400/80">
              <summary className="cursor-pointer">{job.errors.length} error(s)</summary>
              <ul className="mt-1 space-y-0.5">
                {job.errors.slice(0, 30).map((s, i) => (
                  <li key={i} className="truncate" title={`${s.path} — ${s.error}`}>
                    {s.path.split(/[\\/]/).filter(Boolean).slice(-1)[0]} — {s.error}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          value={view}
          onChange={(v) => setView(v)}
          options={[{ id: "albums", label: "Albums" }, { id: "artists", label: "Artists" }]}
        />
        <input
          className="input max-w-xs tap"
          placeholder="Filter by artist or album…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="text-xs text-zinc-500 ml-auto">
          {q ? `${albums.length} of ${data.albums.length} albums` : `${data.albums.length} albums`}
        </span>
      </div>

      {!data.albums.length ? (
        <EmptyState
          title="No MusicBrainz-linked albums yet"
          hint="An album appears here once its files name a MusicBrainz release — an import, script 8 or a tag refresh writes that link."
        />
      ) : view === "artists" ? (
        <div className="panel divide-y divide-border">
          {data.artists.filter((a) => !q || a.artist.toLowerCase().includes(q)).map((a) => (
            <div key={a.artist} className="p-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <span className="font-medium truncate">{a.artist}</span>
              <span className="text-xs text-zinc-500">{a.albums} album(s) · {a.tracks} tracks</span>
              <span className={`text-xs ${a.due ? "text-amber-400" : "text-emerald-500"}`}>
                {a.due ? `${a.due} due` : "up to date"}
              </span>
              <span className="text-xs text-zinc-500">oldest {day(a.oldest)}{a.missing ? ` · ${a.missing} track(s) never pulled` : ""}</span>
              <button
                className="btn-ghost !py-1 text-xs tap ml-auto"
                onClick={() => {
                  setView("albums");
                  setOpenArtist(a.artist);
                  setFilter(a.artist);
                }}
              >
                <ChevronRight className="h-3.5 w-3.5" /> Albums
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {grouped.map(([artist, rows]) => (
            <div key={artist} className="panel">
              <button
                className="w-full flex items-center gap-2 p-3 text-left"
                onClick={() => setOpenArtist(openArtist === artist ? null : artist)}
              >
                {openArtist === artist ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                <Music4 className="h-4 w-4 text-accent" />
                <span className="font-medium text-sm">{artist}</span>
                <span className="text-xs text-zinc-500">{rows.length} album(s)</span>
                {rows.some((r) => r.due) && (
                  <span className="text-xs text-amber-400 ml-auto">{rows.filter((r) => r.due).length} due</span>
                )}
              </button>
              {(openArtist === artist || (q && grouped.length === 1)) && (
                <div className="border-t border-border divide-y divide-border">
                  {rows.map((row) => (
                    <div key={row.path}>
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
                        <button
                          className="inline-flex items-center gap-1 min-w-0 flex-1 text-left"
                          onClick={() => setOpenAlbum(openAlbum === row.path ? null : row.path)}
                          title={row.path}
                        >
                          {openAlbum === row.path ? <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                            : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
                          <span className="truncate">{row.album}</span>
                        </button>
                        <span className={`text-xs shrink-0 ${row.due ? "text-amber-400" : "text-emerald-500"}`}>
                          {row.due ? (row.oldest ? "due" : "never pulled") : "current"}
                        </span>
                        <span className="text-xs text-zinc-500 shrink-0" title={row.oldest ?? "no track carries a stamp"}>
                          {row.tracks} track(s) · oldest {day(row.oldest)}
                          {row.missing ? ` · ${row.missing} never` : ""}
                        </span>
                        <button
                          className="btn-ghost !py-1 text-[11px] tap shrink-0"
                          disabled={running}
                          onClick={() => void startRefresh([row.path], false)}
                          title="Refresh this album now (whole album, one release lookup)"
                        >
                          <RefreshCw className="h-3 w-3" /> Refresh
                        </button>
                      </div>
                      {openAlbum === row.path && <TrackList path={row.path} />}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TrackList({ path }: { path: string }) {
  const q = useQuery<TagAlbumDetail>({
    queryKey: ["tagsAlbum", path],
    queryFn: () => api.tagsAlbum(path),
  });
  if (q.isLoading) return <div className="px-10 py-2 text-xs text-zinc-500"><Loader2 className="inline h-3 w-3 animate-spin" /> reading the files' stamps…</div>;
  if (!q.data) return <div className="px-10 py-2 text-xs text-red-400">{String(q.error)}</div>;
  return (
    <div className="px-10 pb-3">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-zinc-500 text-left">
            <th className="th !py-1">Track</th>
            <th className="th !py-1">MusicBrainz pulled</th>
          </tr>
        </thead>
        <tbody>
          {q.data.tracks.map((tr) => (
            <tr key={tr.path}>
              <td className="td !py-1 truncate max-w-[52ch]" title={tr.path}>{tr.title || tr.path.split("/").pop()}</td>
              <td className="td !py-1">
                {tr.pulled
                  ? <span title={tr.pulled}>{iso(tr.pulled)}</span>
                  : <span className="text-amber-400">never</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-[11px] text-zinc-600 pt-1">
        The album's age is the OLDEST row; a refresh stamps every track with the same pull time, so one unfilled track makes the whole album due.
      </p>
    </div>
  );
}

function Card({
  icon: Icon, label, value, hint, tone = "plain",
}: {
  icon: typeof Database; label: string; value: string; hint?: string;
  tone?: "plain" | "ok" | "warn";
}) {
  return (
    <div className="panel p-3">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-zinc-500">
        <Icon className="h-3.5 w-3.5" /> {label}
      </div>
      <div className={`text-xl font-semibold pt-1 ${tone === "warn" ? "text-amber-400" : tone === "ok" ? "text-emerald-500" : ""}`}>
        {value}
      </div>
      {hint && <div className="text-[11px] text-zinc-600 pt-0.5">{hint}</div>}
    </div>
  );
}