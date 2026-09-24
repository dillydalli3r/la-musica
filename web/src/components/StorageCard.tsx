import { useQuery } from "@tanstack/react-query";
import { HardDrive } from "lucide-react";
import { getToken, serverUrl } from "../api";
import { fmtBytes, fmtPercent } from "../lib/fmt";
import { useI18n } from "../lib/i18n";

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
  /** Every entry the walk stepped over, with the OS's own words. `kind` is
   *  the split that matters on screen: "link" was skipped ON PURPOSE (a file
   *  reached through a link is counted once, at the real file, so nothing is
   *  missing from the figures), "unreadable" is a figure nobody could take. */
  skipped: { path: string; reason: string; kind: "link" | "unreadable" }[];
  skipped_count: number;
  skipped_links: number;
  skipped_unreadable: number;
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
 * then three figures — the library, the app itself (with its state/bin/
 * transfers/tools breakdown on the line under it) and the two of them added
 * up — over the volume's own total/used/free.
 *
 * Polled once a minute — the server walks the tree to answer, so this is a
 * report that refreshes on a schedule, not a live meter. A figure the OS
 * refused ("unknown") is shown as unknown; an absent folder is "—"; neither is
 * ever rendered as "0 bytes", which would read as a full disk. Folders the
 * walk could not read are counted under the rows, so a partial answer says so
 * instead of looking complete.
 */
export default function StorageCard() {
  const { t } = useI18n();
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
  // The Total row adds the two rows ABOVE it from the very figures they show —
  // their byte totals, never the library's audio subtotal or a file count,
  // which are different quantities wearing the same units. It is arithmetic
  // done here, so it cannot disagree with them the way a third number of the
  // server's own could. A row with no reading (no music folder yet, or app
  // folders none of which could be read) leaves the sum unknown: adding to an
  // unknown would be inventing a number.
  const libBytes = data.library ? data.library.bytes : null;
  const appBytes = data.app_total?.measured ? data.app_total.bytes : null;
  const sumBytes = libBytes !== null && appBytes !== null ? libBytes + appBytes : null;
  const sumFiles = sumBytes === null
    ? null
    : (data.library?.files ?? 0) + (data.app_total?.files ?? 0);

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
            addition. Its parts follow on their own line, so this figure and
            its breakdown are one glance instead of five rows. Called "App"
            rather than "App total" because the row BELOW it is the total of
            the two: two rows both named "total" read as the same figure. */}
        <Row
          label="App"
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
        {/* The two rows above, added up HERE rather than asked for a third
            time: the figure on screen is the sum of the two figures on screen,
            so the three rows can never drift apart. The library's `(… audio)`
            subtotal and the App row's file count are not summed — they are not
            the totals. No reading on either side leaves this unknown. */}
        <Row
          label={t("storage.total")}
          value={sumBytes === null ? "—" : fmtBytes(sumBytes)}
          hint={sumFiles === null ? undefined : `(${files(sumFiles)})`}
          title={sumBytes === null
            ? "one of the two rows above has no reading — a total over an unknown would be a guess"
            : `Library ${fmtBytes(libBytes)} + App ${fmtBytes(appBytes)}`}
        />
      </div>

      {data.skipped_unreadable > 0 && (
        <div
          className="text-[10px] text-amber-400/90 leading-relaxed"
          title={data.skipped
            .filter((s) => s.kind !== "link")
            .map((s) => `${s.path} — ${s.reason}`)
            .join("\n")}
        >
          {data.skipped_unreadable} folder{data.skipped_unreadable === 1 ? "" : "s"} could not be
          read — the figures above exclude {data.skipped_unreadable === 1 ? "it" : "them"}.
        </div>
      )}
      {/* Not a warning: a link the walk did not follow has cost the figures
          NOTHING — its target is counted once, at the real file — so this is
          the arithmetic being right, said quietly. (The bundled tools carry a
          few version symlinks; a toolchain unpacked on a network mount is the
          unreadable case above.) */}
      {data.skipped_links > 0 && (
        <div
          className="text-[10px] text-zinc-500 leading-relaxed"
          title={data.skipped
            .filter((s) => s.kind === "link")
            .map((s) => `${s.path} — ${s.reason}`)
            .join("\n")}
        >
          {data.skipped_links} link{data.skipped_links === 1 ? "" : "s"} not followed — a linked
          file or folder is counted once, where it really lives.
        </div>
      )}
    </div>
  );
}
