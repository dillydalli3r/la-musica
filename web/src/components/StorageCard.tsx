import { useQuery } from "@tanstack/react-query";
import { HardDrive } from "lucide-react";
import { getToken, serverUrl } from "../api";
import { fmtBytes, fmtPercent } from "../lib/fmt";

/** One sized folder in the answer: the library, the app's own state, the bin,
 *  a single downloads root. `audio_bytes` / `sidecar_bytes` exist only for the
 *  library (the walk classifies what it counted there). */
interface StorageRow {
  path: string;
  bytes: number;
  files: number;
  audio_bytes?: number;
  sidecar_bytes?: number;
}

/** GET /api/storage — the volume, the library, the app's state, the bin and
 *  the transfer folders (see server/api_storage.py). Every byte figure is a
 *  number; a volume the OS will not measure arrives as `null`, never 0. */
interface StoragePayload {
  mount: string | null;
  label: string | null;
  type: string | null;
  total_bytes: number | null;
  free_bytes: number | null;
  used_bytes: number | null;
  percent_used: number | null;
  library: StorageRow | null;
  app_data: StorageRow | null;
  trash: StorageRow | null;
  /** The app's own tools folder (ffmpeg, slskd, the analysers) — the one part
   *  of the app that does not live under the music folder. */
  dependencies: StorageRow | null;
  /** Everything the app itself occupies: state + bin + transfers + tools, with
   *  `measured: false` when none of those folders could be read (a total of 0
   *  is then "unread", not "empty"). */
  app_total: { bytes: number; files: number; measured: boolean } | null;
  downloads: {
    bytes: number;
    files: number;
    staging_bytes: number;
    staging_files: number;
    roots: StorageRow[];
  } | null;
  skipped: { path: string; reason: string }[];
  skipped_count: number;
  scanned_at: number;
  took_ms: number;
}

async function fetchStorage(): Promise<StoragePayload> {
  // Through `serverUrl()` + the session token rather than a bare relative
  // path: the Tauri phone/desktop shells serve the UI from tauri://localhost
  // and talk to a backend on another address.
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(`${serverUrl()}/api/storage`, {
    credentials: "include",
    headers,
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`storage: HTTP ${r.status}`);
  return (await r.json()) as StoragePayload;
}

function Row({ label, value, hint, title }: {
  label: string;
  value: string;
  hint?: string;
  title?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-xs" title={title}>
      <span className="text-zinc-500 shrink-0">{label}</span>
      <span className="tabular-nums text-zinc-200 text-right truncate">
        {value}
        {hint && <span className="text-zinc-500"> {hint}</span>}
      </span>
    </div>
  );
}

/** "3 files" / "1 file" — a count that reads as a sentence. */
const files = (n: number) => `${n} file${n === 1 ? "" : "s"}`;

/** The app's storage readout: one bar of used/free on the library's volume,
 * then the library, the app's own state, the bin, the transfer folders and the
 * volume's own total/used/free.
 *
 * Polled once a minute — the server walks the tree to answer, so this is a
 * report that refreshes on a schedule, not a live meter. A figure the OS
 * refused ("unknown") is shown as unknown; an absent folder is "—"; neither is
 * ever rendered as "0 bytes", which would read as a full disk. Folders the
 * walk could not read are counted under the rows, so a partial answer says so
 * instead of looking complete.
 */
export default function StorageCard() {
  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["storage"],
    queryFn: fetchStorage,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: 1,
  });

  if (isLoading) {
    return (
      <div className="panel">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          <HardDrive className="h-3.5 w-3.5" /> Storage
        </div>
        <div className="mt-2 text-xs text-zinc-500">Reading disk usage…</div>
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="panel">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          <HardDrive className="h-3.5 w-3.5" /> Storage
        </div>
        <div className="mt-2 text-xs text-zinc-400">
          Storage usage is unavailable: {error instanceof Error ? error.message : String(error)}
        </div>
      </div>
    );
  }

  const { total_bytes: total, free_bytes: free, percent_used: percent } = data;
  // Clamped once: a volume can report a hair over 100 (reserved blocks) and a
  // bar has no room for that.
  const pct = percent === null ? null : Math.min(100, Math.max(0, percent));
  // A drive within a tenth of full is worth saying out loud.
  const tight = pct !== null && pct >= 90;
  const updated = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : "—";
  const dl = data.downloads;

  return (
    <div className="panel space-y-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          <HardDrive className="h-3.5 w-3.5" /> Storage
        </div>
        <span className="text-[11px] text-zinc-500 font-mono" title={data.mount ?? undefined}>
          {data.label ?? data.mount ?? "—"}
          {data.type && data.type !== "fixed" ? ` · ${data.type}` : ""}
        </span>
        <span
          className="ml-auto text-[10px] text-zinc-500"
          title={`Updated ${updated} · re-read every 60 s (this read took ${data.took_ms} ms)`}
        >
          updated {updated}
        </span>
      </div>

      {/* Used vs free of the whole volume. It is a bar of the DISK, not of the
          app: the Library and App total rows below are what la musica itself
          accounts for, and the exact disk figures ride this line's tooltip. */}
      <div className="space-y-1">
        <div className="h-1.5 rounded-sm bg-raise overflow-hidden">
          {/* An unmeasurable volume gets a live-looking placeholder rather
              than a 0%-wide fill, which reads as an empty disk instead of an
              unread one (same shape as ProgressBar's unknown state). */}
          <div
            className={`h-full transition-all duration-300 ${pct === null
              ? "w-1/3 animate-pulse bg-zinc-600"
              : tight
                ? "bg-gradient-to-r from-amber-500 to-red-500"
                : "bg-gradient-to-r from-accent to-accent-soft"}`}
            style={pct === null ? undefined : { width: `${pct}%` }}
          />
        </div>
        <div
          className="flex items-baseline justify-between gap-2 text-[11px]"
          title={`${fmtBytes(total)} total · ${fmtBytes(data.used_bytes)} used · ${fmtBytes(free)} free`}
        >
          <span className={`tabular-nums ${tight ? "text-red-400" : "text-zinc-400"}`}>
            {pct === null ? "used space unknown" : `${fmtPercent(pct)} used`}
          </span>
          <span className="text-zinc-500 tabular-nums">
            {free === null || total === null
              ? "unknown free of unknown total"
              : `${fmtBytes(free)} free of ${fmtBytes(total)}`}
          </span>
        </div>
      </div>

      <div className="space-y-1.5 border-t border-border pt-2">
        <Row
          label="Library"
          value={data.library ? fmtBytes(data.library.bytes) : "—"}
          hint={data.library?.audio_bytes
            ? `(${fmtBytes(data.library.audio_bytes)} audio)`
            : undefined}
          title={data.library
            ? `${files(data.library.files)} under ${data.library.path}`
            : "no library folder yet (the music folder is not set)"}
        />
        {/* Everything the app itself keeps, in ONE figure: its state, the bin,
            the transfers and its own tools. The library above is the user's
            music and deliberately not part of it — "how much is la musica
            using" is the question the per-folder rows answered only by
            addition. Its parts follow on their own line, so the total and the
            breakdown are one glance instead of five rows. */}
        <Row
          label="App total"
          value={data.app_total?.measured ? fmtBytes(data.app_total.bytes) : "—"}
          hint={data.app_total?.measured ? `(${files(data.app_total.files)})` : undefined}
          title={[
            `State ${data.app_data ? fmtBytes(data.app_data.bytes) : "—"}  ${data.app_data?.path ?? ""}`,
            `Bin ${data.trash ? fmtBytes(data.trash.bytes) : "—"}  ${data.trash?.path ?? "no bin yet"}`,
            `Transfers ${dl ? fmtBytes(dl.bytes) : "—"}${dl?.staging_bytes ? `  (${fmtBytes(dl.staging_bytes)} still staging)` : ""}`,
            `Tools ${data.dependencies ? fmtBytes(data.dependencies.bytes) : "—"}  ${data.dependencies?.path ?? "no tools installed"}`,
          ].join("\n")}
        />
        {data.app_total?.measured && (
          <div className="text-[10px] text-zinc-500 leading-snug pl-1">
            {[
              data.app_data ? `state ${fmtBytes(data.app_data.bytes)}` : "",
              data.trash ? `bin ${fmtBytes(data.trash.bytes)}` : "",
              dl ? `transfers ${fmtBytes(dl.bytes)}${dl.staging_bytes ? ` (${fmtBytes(dl.staging_bytes)} staging)` : ""}` : "",
              data.dependencies ? `tools ${fmtBytes(data.dependencies.bytes)}` : "",
            ]
              .filter(Boolean)
              .join(" · ")}
          </div>
        )}
      </div>

      {data.skipped_count > 0 && (
        <div
          className="text-[10px] text-amber-400/90 leading-relaxed"
          title={data.skipped.map((s) => `${s.path} — ${s.reason}`).join("\n")}
        >
          {data.skipped_count} folder{data.skipped_count === 1 ? "" : "s"} could not be read — the
          figures above exclude {data.skipped_count === 1 ? "it" : "them"}.
        </div>
      )}
    </div>
  );
}
