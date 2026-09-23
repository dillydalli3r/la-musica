import { useState } from "react";
import type { ReactNode } from "react";
import { X } from "lucide-react";

import { fmtCounts, fmtSteps } from "../lib/fmt";
import { progressKey, type ProgressEntry, type ProgressSample } from "../store";

/** What a producer is called on its row when its frames carry no label: the
 *  relay's own kinds (the export engine, a run, a script chain — and an import
 *  chain, which is a run the user started from Soulseek). */
const KIND_LABEL: Record<string, string> = {
  export: "Exporting",
  run: "Run",
  import: "Importing",
  script: "Script",
};

/** Row order in the stack: the export first (it is the run a user starts and
 *  watches, and the only one with a control), then runs and imports, then
 *  scripts. Anything else — the id-less legacy frame included — sorts last,
 *  and ties keep insertion order (`Object.values` plus a stable sort), so a
 *  row never jumps position while it is updating. */
const KIND_RANK: Record<string, number> = { export: 0, run: 1, import: 1, script: 2 };

/** One bar: spinner + the producer's label + its own text + a thin bar + the
 *  readout. Replaces the old full-width progress row.
 *
 * `done` may be fractional (bytes of a download, a script's own sub-step):
 * the bar draws that continuous share, while the readout stays a whole
 * number — for a chained script run the frame carries the step pair, so it
 * reads "3/18" (scripts) instead of "2.9/18". */
function ProgressRow({
  label,
  progress,
  action,
}: {
  label?: string;
  progress: ProgressSample;
  action?: ReactNode;
}) {
  const known = !!progress.total;
  const pct = progress.total ? Math.min(100, (progress.done / progress.total) * 100) : 0;
  const steps = fmtSteps(progress.steps);
  return (
    <div className="flex items-center gap-2 min-w-0 w-full max-w-md">
      <span className="h-3 w-3 rounded-full border-2 border-zinc-700 border-t-accent-soft animate-spin shrink-0" />
      {label && (
        <span className="text-[11px] text-zinc-300 font-medium truncate max-w-[140px] shrink-0" title={label}>
          {label}
        </span>
      )}
      <span className="text-[11px] text-zinc-400 truncate min-w-0 max-w-[180px]" title={progress.desc}>
        {progress.desc}
      </span>
      <div className="h-1 flex-1 min-w-[64px] rounded-sm bg-raise overflow-hidden">
        <div
          className={`h-full bg-gradient-to-r from-accent to-accent-soft transition-all duration-300 ${known ? "" : "w-1/3 animate-pulse"}`}
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
      {action}
    </div>
  );
}

/** Compact live progress for the top bar's free space — ONE bar, the shape the
 *  pages' own strips draw (the optimization page, the import wizard). The
 *  shell's cluster draws the whole stack: see `ProgressStack`. */
export function ProgressInline({ progress }: { progress: ProgressSample | null }) {
  if (!progress) return null;
  return <ProgressRow progress={progress} />;
}

/** Every live producer's bar, stacked. Each row names what it belongs to, so
 *  an export and a run can be on screen together — a bar replacing the other
 *  is the bug this replaces. */
export function ProgressStack({
  entries,
  onCancelExport,
}: {
  entries: ProgressEntry[];
  onCancelExport: () => Promise<boolean>;
}) {
  const rows = [...entries].sort((a, b) => (KIND_RANK[a.kind] ?? 3) - (KIND_RANK[b.kind] ?? 3));
  return (
    <>
      {rows.map((e) => (
        <ProgressRow
          // The store's own key: the producer's id, or its kind for the
          // id-less legacy frame — which is also what keeps a row's pressed
          // cancel button on the row it belongs to while frames land.
          key={progressKey(e)}
          label={e.label || KIND_LABEL[e.kind] || "Working"}
          progress={e}
          action={e.kind === "export" ? <ExportCancel press={onCancelExport} /> : undefined}
        />
      ))}
    </>
  );
}

/** The export row's stop control. The server checks its own flag at the next
 *  file boundary and KEEPS everything already written, so a press is not
 *  instant: `press` answers whether the server took it — a taken cancel
 *  leaves the button pressed until the bar itself goes (`progress_end`, or
 *  the lock list dropping the job), a refusal re-arms it. Only the export has
 *  one: no run or script has a stop the server honours. */
function ExportCancel({ press }: { press: () => Promise<boolean> }) {
  const [pressed, setPressed] = useState(false);
  return (
    <button
      type="button"
      className="shrink-0 -mr-0.5 p-0.5 rounded tap text-zinc-500 hover:text-white hover:bg-raise disabled:opacity-60 disabled:cursor-default"
      title={
        pressed
          ? "Stopping at the next file…"
          : "Cancel the export: it stops at the next file and everything already written is kept"
      }
      aria-label="Cancel export"
      disabled={pressed}
      onClick={() => {
        setPressed(true);
        void press()
          .then(setPressed)
          .catch(() => setPressed(false));
      }}
    >
      <X className="h-3.5 w-3.5" />
    </button>
  );
}
