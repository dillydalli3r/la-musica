/** The rip log itself, in a panel: what Logchecker says, what this app says
 *  about the log's checksum, and the file's own text.
 *
 *  The owner's report, twice over: an auto-import rejected seventeen candidates
 *  for an album with "log rejected: … score 60 is below the required 100", and
 *  there was no way in the app to see WHICH log, what Logchecker deducted, or
 *  the log itself ("I don't even know a way to view logs"). A score is a
 *  verdict; Logchecker's own `Details:` lines are where the deduction explains
 *  itself, and the text is where a person checks the claim.
 *
 *  Everything shown is served by `GET /api/log/report` — the phar's real output
 *  parsed on the server, never re-derived here — and `available: false` is said
 *  out loud rather than drawn as a score of zero (that is "no scorer
 *  installed", a different fact from "this log is bad"). */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileText, Loader2 } from "lucide-react";
import { api } from "../api";

/** The tone of one checksum state, in the app's own chip colours. `unverified`
 *  is amber and not green: the log carries a checksum that nothing could
 *  check, which is what a missing EAC log checker looks like. */
const CHECKSUM_TONE: Record<string, string> = {
  ok: "bg-emerald-900/40 text-emerald-300 border-emerald-800",
  invalid: "bg-red-900/40 text-red-300 border-red-800",
  unverified: "bg-amber-900/40 text-amber-300 border-amber-800",
};

const CHECKSUM_WORDS: Record<string, string> = {
  ok: "checksum verified",
  invalid: "checksum does NOT match",
  unverified: "checksum not verified",
  missing: "no checksum in the log",
  unsupported: "this ripper writes no checksum",
};

export default function LogReport({ albumPath, disc, onClose }: {
  /** The album folder, or the .log itself. */
  albumPath: string;
  /** Which disc, for a multi-disc album (`CD-<n>.log`). */
  disc?: number;
  onClose?: () => void;
}) {
  const [wanted, setWanted] = useState<number | undefined>(disc);
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["logReport", albumPath, wanted ?? 0],
    queryFn: () => api.logReport(albumPath, wanted),
    staleTime: 60_000,
  });

  const checksum = data?.checksum?.state ?? "";
  const score = data?.report?.score;
  const title = data?.name || "rip log";

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-zinc-300">
          <FileText className="h-3.5 w-3.5" /> {title}
        </span>
        {data && (
          <>
            <span className={`chip border ${score != null && score >= 100
              ? "bg-emerald-900/40 text-emerald-300 border-emerald-800"
              : "bg-amber-900/40 text-amber-300 border-amber-800"}`}
              title="Logchecker's own score for this log">
              score {score == null ? "—" : score}/100
            </span>
            <span className={`chip border ${CHECKSUM_TONE[checksum] ?? "bg-raise border-border text-zinc-400"}`}
              title={data.checksum?.detail ?? undefined}>
              {CHECKSUM_WORDS[checksum] ?? (checksum || "checksum unknown")}
            </span>
            {data.report?.ripper && (
              <span className="chip bg-raise border border-border text-zinc-400">
                {data.report.ripper}{data.report.version ? ` ${data.report.version}` : ""}
              </span>
            )}
            <span className="text-[10px] text-zinc-600">
              {(data.bytes / 1024).toFixed(1)} KB{data.truncated ? " (text capped)" : ""}
            </span>
          </>
        )}
        <div className="ml-auto flex items-center gap-2">
          {/* More than one log in the folder is a disc set: the payload lists
              them, so switching is one click and no second guess. */}
          {(data?.siblings?.length ?? 0) > 1 && (
            <span className="flex items-center gap-1">
              {data!.siblings.map((name, i) => (
                <button key={name}
                  className={`chip border ${(wanted ?? 1) === i + 1
                    ? "bg-accent/20 text-accent-soft border-accent/40"
                    : "bg-raise border-border text-zinc-400 hover:text-zinc-200"}`}
                  onClick={() => setWanted(i + 1)}
                  title={`Read ${name}`}>
                  {i + 1}
                </button>
              ))}
            </span>
          )}
          <button className="btn-ghost !py-1 text-xs tap" onClick={() => refetch()} disabled={isFetching}
            title="Score it again — the log may have changed, or a tool may have been installed since">
            {isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Re-check
          </button>
          {onClose && (
            <button className="btn-ghost !py-1 text-xs tap" onClick={onClose}>Close</button>
          )}
        </div>
      </div>

      {isLoading && (
        <div className="flex items-center gap-2 text-xs text-zinc-500">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Reading the log…
        </div>
      )}
      {error != null && (
        <div className="text-xs text-red-300">
          {error instanceof Error ? error.message : String(error)}
        </div>
      )}

      {data && !data.available && (
        <div className="text-xs text-amber-300/90 bg-amber-950/20 border border-amber-900/40 rounded px-2 py-1.5">
          No rip-log scorer is installed (PHP + Logchecker), so nothing graded
          this log — that is not a score of zero. Dependencies → install
          Logchecker, then press Re-check.
        </div>
      )}

      {data?.available && (
        <>
          {data.report.details.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1">
                What Logchecker says about it
              </div>
              <ul className="space-y-1">
                {data.report.details.map((line, i) => (
                  <li key={i}
                    className={`text-xs rounded border px-2 py-1 ${/error|refus|invalid|missing/i.test(line)
                      ? "text-red-300/90 bg-red-950/30 border-red-900/40"
                      : "text-zinc-300/90 bg-raise/40 border-border/60"}`}>
                    {line}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {data.checksum?.detail && (
            <div className="text-[11px] text-zinc-500">
              This app's own check: {data.checksum.detail}
            </div>
          )}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1">
              The log itself
            </div>
            <pre className="max-h-[46vh] overflow-auto rounded border border-border bg-black/40 px-3 py-2 text-[11px] leading-relaxed text-zinc-300 whitespace-pre-wrap font-mono">
              {data.text}
            </pre>
          </div>
          <details className="text-[11px] text-zinc-500">
            <summary className="cursor-pointer">Logchecker's raw output</summary>
            <pre className="mt-1 max-h-40 overflow-auto rounded border border-border bg-black/40 px-3 py-2 text-[10px] whitespace-pre-wrap font-mono">
              {data.report.raw}
            </pre>
          </details>
        </>
      )}
    </div>
  );
}
