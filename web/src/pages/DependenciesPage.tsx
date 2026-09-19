import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen, RotateCcw, Wrench } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import PageHeader from "../components/PageHeader";
import { EmptyState } from "../components/Badges";

type DepTool = {
  key: string;
  name: string;
  installed_version?: string;
  latest_version?: string;
  detected_version?: string;
  path?: string | null;
  state: string; // "ok" | "update" | "missing" | "error"
  upstream_version?: string | null;
  upstream_checked_at?: string | null;
  update_available?: boolean;
  note?: string | null;
};

/** Sidebar "Dependencies" — the external binaries the scripts shell out to
 * (ffmpeg, yt-dlp, beets…). Mirrors the setup wizard's tool check, but always
 * reachable: inspect versions, pin installs into the app's .dependencies
 * folder, and pull updates in one click. */
export default function DependenciesPage() {
  const [busy, setBusy] = useState(false);
  // Refresh asks the backend to re-check GitHub for THIS fetch only
  // (?refresh=1); the normal fetch answers from its 30-minute cache.
  const forceRef = useRef(false);
  const { data: deps, isLoading, refetch } = useQuery({
    queryKey: ["dependencies"],
    queryFn: () => api.dependencies(forceRef.current),
  });
  const refreshNow = () => {
    forceRef.current = true;
    refetch().finally(() => {
      forceRef.current = false;
    });
  };

  // The upstream check runs in the backend's background thread: poll while it
  // is in flight, or the version the user just asked for never appears.
  useEffect(() => {
    if (!deps?.checking) return;
    const t = setInterval(() => refetch(), 3000);
    return () => clearInterval(t);
  }, [deps?.checking, refetch]);

  const tools: DepTool[] = deps?.tools ?? [];
  const missing = tools.filter((t) => t.state === "missing");
  const updates = tools.filter((t) => t.state === "update");
  const ready = tools.filter((t) => t.state === "ok").length;
  // The tool list is empty only when the payload could not be read at all —
  // that case is the page's empty state, not a one-row table.
  const noTools = !isLoading && tools.length === 0;

  /** Path relative to the deps dir when possible — the full prefix repeats on
   * every row, so show the distinguishing tail ("…/flac v1.5.0/flac.exe"). */
  const shortPath = (p: string) => {
    const d = String(deps?.deps_dir ?? "").replace(/[\\/]+$/, "");
    if (d && p.toLowerCase().startsWith(d.toLowerCase())) {
      const rel = p.slice(d.length).replace(/^[\\/]+/, "").replace(/\\/g, "/");
      if (rel) return `.../${rel}`;
    }
    return p.replace(/\\/g, "/");
  };

  const install = async (keys?: string[]) => {
    setBusy(true);
    try {
      const r = await api.installDependencies(keys);
      const failed = r.results.filter((x) => !x.ok);
      if (failed.length) toast.error(`Install finished with ${failed.length} failure(s): ${failed.map((f) => f.name).join(", ")}`);
      else toast.success("Dependencies installed / updated");
      refetch();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const openDepsDir = async () => {
    if (!deps?.deps_dir) return;
    try {
      await api.openFolder(deps.deps_dir);
    } catch (e) {
      toast.error(String(e));
    }
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={Wrench}
        title="Dependencies"
        subtitle="External tools the scripts rely on. Missing ones are downloaded into the app's dependencies folder — nothing is installed system-wide."
        actions={
          <>
            <button className="btn-ghost !py-1 text-xs" onClick={refreshNow} disabled={busy || isLoading}>
              <RotateCcw className="h-3 w-3" /> Refresh
            </button>
            {(missing.length > 0 || updates.length > 0) && (
              <button
                className="btn-ghost !py-1 text-xs"
                onClick={() => install([...missing, ...updates].map((t) => t.key))}
                disabled={busy}
              >
                Install {missing.length + updates.length} ({missing.length} missing · {updates.length} updates)
              </button>
            )}
            <button className="btn-primary !py-1 text-xs" onClick={() => install()} disabled={busy}>
              {busy ? "Installing…" : "Install / update all"}
            </button>
          </>
        }
      />

      <div className="flex items-center gap-4 text-[11px] text-zinc-500">
        <span>
          {isLoading ? "Checking tools…" : `${ready}/${tools.length} ready`}
          {updates.length > 0 && <span className="text-amber-400"> · {updates.length} update(s) available</span>}
          {missing.length > 0 && <span className="text-red-400"> · {missing.length} missing</span>}
          {deps?.checking && <span className="text-zinc-400"> · checking upstream…</span>}
        </span>
        {deps?.deps_dir && (
          <button
            className="inline-flex items-center gap-1 hover:text-zinc-300 transition-colors font-mono truncate max-w-[24rem]"
            onClick={openDepsDir}
            title="Open the dependencies folder"
          >
            <FolderOpen className="h-3 w-3 shrink-0" />
            {String(deps.deps_dir).replace(/\\/g, "/")}
          </button>
        )}
      </div>

      {noTools && (
        <EmptyState
          title="No tools reported"
          hint="Could not read the tool list — is the backend running? Refresh once it answers."
        />
      )}

      {!noTools && (
      <div className="rounded-lg border border-border overflow-hidden table-scroll">
        <table className="w-full text-sm">
          <thead className="bg-panel/60">
            <tr>
              <th className="th">Tool</th>
              <th className="th">Status</th>
              <th className="th">Installed</th>
              <th className="th" title="The version the installer fetches for this tool — on Linux a distro-provided tool shows its system package instead">
                Latest
              </th>
              <th className="th" title="Newest release published upstream on GitHub. The installer still fetches the reviewed version in Latest.">
                Available
              </th>
              <th className="th">Location</th>
            </tr>
          </thead>
          <tbody className="stagger">
            {tools.map((t) => (
              <tr key={t.key} className="table-row cursor-default">
                <td className="td font-medium">{t.name}</td>
                <td className="td">
                  {t.state === "ok" && (
                    <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">Ready</span>
                  )}
                  {t.state === "update" && (
                    <span
                      className="chip bg-amber-900/50 text-amber-300 border border-amber-900"
                      title={t.note ?? (t.upstream_version ? `Upstream: ${t.upstream_version}` : undefined)}
                    >
                      Update
                    </span>
                  )}
                  {t.state === "missing" && (
                    <span className="chip bg-red-900/50 text-red-300 border border-red-900">Missing</span>
                  )}
                  {t.state === "error" && (
                    <span className="chip bg-zinc-800 text-zinc-400 border border-zinc-700" title={t.note ?? undefined}>
                      Check failed
                    </span>
                  )}
                </td>
                <td className="td text-zinc-500">
                  {t.installed_version ?? t.detected_version ?? "—"}
                </td>
                <td className="td text-zinc-500">
                  {t.latest_version || <span className="text-zinc-600 italic">Unknown</span>}
                </td>
                <td className="td text-zinc-500" title={t.note ?? ""}>
                  {t.upstream_version
                    ? <span className={t.update_available ? "text-amber-300" : undefined}>{t.upstream_version}</span>
                    : deps?.checking
                      ? <span className="text-zinc-600 italic">Checking…</span>
                      : <span className="text-zinc-600 italic">Unknown</span>}
                </td>
                <td className="td text-[11px] text-zinc-600 font-mono truncate" title={t.path ?? ""}>
                  {t.path ? shortPath(t.path) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}

      <div className="text-[10px] text-zinc-600">
        Install downloads the pinned release from GitHub into the dependencies folder; PATH-installed tools
        (scoop etc.) are shown as ready. <span className="text-zinc-500">Available</span> is what upstream has
        published — the installer waits for the pin to be reviewed, so Install fetches Latest, not Available.
        {deps?.note && <span className="text-amber-500"> Upstream check: {deps.note}</span>}
        {deps?.upstream_checked_at && (
          <span> Checked {new Date(deps.upstream_checked_at).toLocaleTimeString()}.</span>
        )}
      </div>
    </div>
  );
}
