import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, ArrowDownToLine, Disc3, FolderInput, FolderOpen, RefreshCw, Trash2, X,
} from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import { EmptyState } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import type { DownloadEntry, ImportBulkJob } from "../types";
import { fmtCounts } from "../lib/fmt";

/** Human byte size. Local deliberately: the player bar's formatter is tuned for
 *  audio readouts, and these are multi-GB folder totals. */
function fmtSize(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** `<music>/.mlo/downloads` — slskd's staging area.
 *
 *  Everything Soulseek pulls down lands here and stays until it is imported
 *  into the library or deleted, so this is where a finished download is
 *  triaged. Entries are folders (usually one album each) or loose files;
 *  selecting several acts on all of them at once. */
export default function DownloadsPage() {
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
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={ArrowDownToLine}
        title="Downloads"
        subtitle={
          <>
            {data?.count ?? 0} entr{(data?.count ?? 0) === 1 ? "y" : "ies"} · {fmtSize(data?.bytes ?? 0)}
            {totals.albums > 0 && ` · ${totals.albums} look like albums`}
            {totals.partial > 0 && ` · ${totals.partial} in-flight`}
          </>
        }
        actions={
          <button
            className="btn-ghost !py-1 text-xs"
            onClick={() => refetch()}
            disabled={isFetching}
            title="Re-read the downloads folder"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> Refresh
          </button>
        }
      />

      {data?.folder && (
        <div className="text-[11px] text-zinc-600 font-mono truncate" title={data.folder}>
          {data.folder}
        </div>
      )}

      {/* Bulk import (2+ entries): per-album progress of the queue. */}
      {bulkJob?.status === "running" && (
        <div className="panel space-y-1.5">
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
              className="h-full bg-gradient-to-r from-accent to-indigo-500 transition-all duration-300"
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
        <div className="panel border-red-900/60 text-xs text-red-300 space-y-0.5">
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
          hint="Soulseek downloads land here, then you import or delete them from this page."
        />
      )}

      {entries.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <button
            className="btn-ghost !py-1 text-xs"
            onClick={() => setSel(allSelected ? new Set() : new Set(entries.map((e) => e.name)))}
          >
            {allSelected ? "Select none" : "Select all"}
          </button>
          {liveSelection.length > 0 && (
            <span className="text-xs text-zinc-400">{liveSelection.length} selected</span>
          )}
          <div className="flex items-center gap-2 ml-auto">
            <button
              className="btn-primary !py-1 text-xs"
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
                <button className="btn-danger !py-1 text-xs" onClick={doDelete} disabled={busy}>
                  <Trash2 className="h-3.5 w-3.5" /> Yes, delete
                </button>
                <button className="btn-ghost !py-1 text-xs" onClick={() => setConfirmDelete(false)} disabled={busy}>
                  <X className="h-3.5 w-3.5" /> Cancel
                </button>
              </>
            ) : (
              <button
                className="btn-danger !py-1 text-xs"
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
  );
}
