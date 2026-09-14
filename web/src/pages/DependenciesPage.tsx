import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen, RotateCcw, Wrench } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";

type DepTool = {
  key: string;
  name: string;
  installed_version?: string;
  latest_version?: string;
  detected_version?: string;
  path?: string | null;
  state: string; // "ok" | "update" | "missing"
};

/** Sidebar "Dependencies" — the external binaries the scripts shell out to
 * (ffmpeg, yt-dlp, beets…). Mirrors the setup wizard's tool check, but always
 * reachable: inspect versions, pin installs into the app's .dependencies
 * folder, and pull updates in one click. */
export default function DependenciesPage() {
  const [busy, setBusy] = useState(false);
  const { data: deps, isLoading, refetch } = useQuery({
    queryKey: ["dependencies"],
    queryFn: api.dependencies,
  });

  const tools: DepTool[] = deps?.tools ?? [];
  const missing = tools.filter((t) => t.state === "missing");
  const updates = tools.filter((t) => t.state === "update");
  const ready = tools.filter((t) => t.state === "ok").length;

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
      toast(
        failed.length
          ? `Install finished with ${failed.length} failure(s): ${failed.map((f) => f.name).join(", ")}`
          : "Dependencies installed / updated"
      );
      refetch();
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  const openDepsDir = async () => {
    if (!deps?.deps_dir) return;
    try {
      await api.openFolder(deps.deps_dir);
    } catch (e) {
      toast(String(e));
    }
  };

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <Wrench className="h-6 w-6 text-accent" /> Dependencies
          </h1>
          <p className="text-xs text-zinc-400 mt-1 max-w-lg">
            External tools the scripts rely on. Missing ones are downloaded into the app's dependencies
            folder — nothing is installed system-wide.
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost !py-1 text-xs" onClick={() => refetch()} disabled={busy || isLoading}>
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
        </div>
      </div>

      <div className="flex items-center gap-4 text-[11px] text-zinc-500">
        <span>
          {isLoading ? "Checking tools…" : `${ready}/${tools.length} ready`}
          {updates.length > 0 && <span className="text-amber-400"> · {updates.length} update(s) available</span>}
          {missing.length > 0 && <span className="text-red-400"> · {missing.length} missing</span>}
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

      <div className="rounded-lg border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-panel/60">
            <tr>
              <th className="th">Tool</th>
              <th className="th">Status</th>
              <th className="th">Version</th>
              <th className="th">Location</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((t) => (
              <tr key={t.key} className="table-row cursor-default">
                <td className="td font-medium">{t.name}</td>
                <td className="td">
                  {t.state === "ok" && (
                    <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">ready</span>
                  )}
                  {t.state === "update" && (
                    <span className="chip bg-amber-900/50 text-amber-300 border border-amber-900">update</span>
                  )}
                  {t.state === "missing" && (
                    <span className="chip bg-red-900/50 text-red-300 border border-red-900">missing</span>
                  )}
                </td>
                <td className="td text-zinc-500">
                  {t.installed_version ?? t.detected_version ?? "—"}
                  {t.state === "update" && t.latest_version && (
                    <span className="text-amber-400/80"> → {t.latest_version}</span>
                  )}
                </td>
                <td className="td text-[11px] text-zinc-600 font-mono truncate" title={t.path ?? ""}>
                  {t.path ? shortPath(t.path) : "—"}
                </td>
              </tr>
            ))}
            {!isLoading && tools.length === 0 && (
              <tr>
                <td className="td text-zinc-500" colSpan={4}>
                  Could not read the tool list — is the backend running?
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
