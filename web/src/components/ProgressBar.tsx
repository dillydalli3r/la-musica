import { fmtCounts, fmtSteps } from "../lib/fmt";

/** Compact live progress for the top bar's free space: spinner + label + a
 * thin bar + the readout. Replaces the old full-width progress row.
 *
 * `done` may be fractional (bytes of a download, a script's own sub-step):
 * the bar draws that continuous share, while the readout stays a whole
 * number — for a chained script run the frame carries the step pair, so it
 * reads "3/18" (scripts) instead of "2.9/18". */
export function ProgressInline({ progress }: { progress: { done: number; total: number; desc: string; steps?: number[] } | null }) {
  if (!progress) return null;
  const known = !!progress.total;
  const pct = progress.total ? Math.min(100, (progress.done / progress.total) * 100) : 0;
  const steps = fmtSteps(progress.steps);
  return (
    <div className="flex items-center gap-2 min-w-0 w-full max-w-md">
      <span className="h-3 w-3 rounded-full border-2 border-zinc-700 border-t-accent-soft animate-spin shrink-0" />
      <span className="text-[11px] text-zinc-400 truncate max-w-[180px]" title={progress.desc}>
        {progress.desc}
      </span>
      <div className="h-1 flex-1 min-w-[64px] rounded-sm bg-raise overflow-hidden">
        <div
          className={`h-full bg-gradient-to-r from-accent to-indigo-500 transition-all duration-300 ${known ? "" : "w-1/3 animate-pulse"}`}
          style={known ? { width: `${pct}%` } : undefined}
        />
      </div>
      {known && (
        <span
          className="text-[10px] text-zinc-500 font-mono whitespace-nowrap tabular-nums"
          title={steps ? "steps finished / steps in this run" : "files done / total"}
        >
          {steps ?? fmtCounts(progress.done, progress.total)}
        </span>
      )}
    </div>
  );
}
