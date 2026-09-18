import { fmtCounts } from "../lib/fmt";

/** Compact live progress for the top bar's free space: spinner + label + a
 * thin bar + done/total. Replaces the old full-width progress row.
 *
 * `done` may be fractional (bytes of a download, a script's own sub-step):
 * the bar has always drawn the continuous share, and the readout prints the
 * fraction rather than a rounded integer. */
export function ProgressInline({ progress }: { progress: { done: number; total: number; desc: string } | null }) {
  if (!progress) return null;
  const known = !!progress.total;
  const pct = progress.total ? Math.min(100, (progress.done / progress.total) * 100) : 0;
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
        <span className="text-[10px] text-zinc-500 font-mono whitespace-nowrap tabular-nums" title="files done / total">
          {fmtCounts(progress.done, progress.total)}
        </span>
      )}
    </div>
  );
}
