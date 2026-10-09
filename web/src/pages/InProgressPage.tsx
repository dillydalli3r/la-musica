import { Activity, Lock } from "lucide-react";
import type { JobLock } from "../api";
import PageHeader from "../components/PageHeader";
import { EmptyState, PageLoading } from "../components/Badges";
import { fmtCounts, fmtSteps } from "../lib/fmt";
import { kindLabel, useJobLocks } from "../lib/locks";

/** Elapsed seconds -> "42s" / "3m 07s" / "1h 12m". The server measures this,
 *  so a client whose clock is off still shows the true run time. */
function fmtElapsed(sec: number): string {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** One in-flight job: what it is, how far it has got, and the folders it holds. */
function JobRow({ job }: { job: JobLock }) {
  const progress = job.progress;
  // A chained run writes the step pair beside its fraction (the same frame the
  // header bar gets), so the row reads "3/18" — scripts — exactly like the bar
  // instead of printing the fractional position as a count.
  const steps = fmtSteps(progress?.steps);
  const started = job.started_at ? new Date(job.started_at * 1000).toLocaleTimeString() : "";
  return (
    <li className="panel space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="chip bg-accent/15 border border-accent/30 text-accent">
          {kindLabel(job.kind)}
        </span>
        <span className="text-sm font-medium text-zinc-200 min-w-0 truncate">{job.label}</span>
        <span className="ml-auto text-[11px] text-zinc-500 tabular-nums" title={started ? `started ${started}` : job.job}>
          running {fmtElapsed(job.elapsed)}
        </span>
      </div>

      {progress && (
        <div className="space-y-1">
          <div className="flex items-center justify-between gap-3 text-[11px] text-zinc-500">
            <span className="truncate" title={progress.text}>{progress.text || "working"}</span>
            {!!progress.total && (
              <span
                className="tabular-nums shrink-0"
                title={steps ? "steps finished / steps in this run" : "files done / total"}
              >
                {steps ?? fmtCounts(progress.done, progress.total)}
              </span>
            )}
          </div>
          <div className="h-1 rounded-sm bg-raise overflow-hidden">
            <div
              className={`h-full bg-gradient-to-r from-accent to-accent-soft transition-all duration-300 ${
                progress.total ? "" : "w-1/3 animate-pulse"
              }`}
              style={progress.total ? { width: `${Math.min(100, (progress.done / progress.total) * 100)}%` } : undefined}
            />
          </div>
        </div>
      )}

      <div className="text-[11px] text-zinc-500 space-y-0.5">
        {job.paths.length === 0 ? (
          <div className="flex items-center gap-1">
            <Lock className="h-3 w-3 shrink-0" />
            no folder held right now — between steps
          </div>
        ) : (
          <>
            <div className="flex items-center gap-1">
              <Lock className="h-3 w-3 shrink-0" />
              {job.paths.length === 1 ? "locked folder" : `${job.paths.length} locked folders`}
            </div>
            {job.paths.map((p) => (
              <div key={p} className="truncate pl-4 font-mono" title={p}>
                {String(p).replace(/\\/g, "/")}
              </div>
            ))}
          </>
        )}
      </div>
    </li>
  );
}

/** Sidebar MAINTAIN → In progress: the library jobs running right now and the
 *  paths each one holds, from GET /api/jobs/locks (server.job_locks).
 *
 *  This is the read-out of the library lock: while a job is listed here,
 *  deleting, moving, organizing or retagging any folder it holds is refused
 *  (409) by the backend instead of racing the job. There is no "force
 *  release": the claim belongs to the work holding it, so dropping it here
 *  would only mean the files are unprotected while the job is still writing
 *  to them — a job that must stop is stopped where it started. */
export default function InProgressPage() {
  // The app's one lock poll (lib/locks): the same payload the player bar and
  // every marked row read, so this page can never show a different answer than
  // the player got — and it costs no extra request.
  const { data, isLoading } = useJobLocks();
  const jobs = data?.jobs ?? [];
  return (
    <div className="p-6 space-y-5 mx-auto max-w-5xl">
      <PageHeader
        icon={Activity}
        title="In progress"
        subtitle="Library work running now, and the files it has locked — nothing else can delete, move or retag those paths until the job finishes."
        chips={jobs.length ? [`${jobs.length} running`] : undefined}
      />
      {isLoading ? (
        <PageLoading label="Checking what is running…" />
      ) : jobs.length === 0 ? (
        <EmptyState
          title="Nothing is running"
          hint="Script runs, imports, organizes and tag writes appear here while they hold library files."
        />
      ) : (
        <ul className="space-y-3">
          {jobs.map((j) => (
            <JobRow key={j.job} job={j} />
          ))}
        </ul>
      )}
    </div>
  );
}
