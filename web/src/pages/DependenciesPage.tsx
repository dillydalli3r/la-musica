import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen, RotateCcw, Wrench } from "lucide-react";
import { api, deviceUnavailable, installSummary, unavailableFeatures } from "../api";
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
  /** False when this host has nothing to fetch for the tool (a Windows-only
   *  one on Linux, or a distro package the image already provides). */
  installable?: boolean;
  /** The sentence explaining that, shown on the row. */
  install_note?: string | null;
  install_kind?: "deps" | "system" | "unsupported";
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
    // The upstream check runs in the backend's background thread, so the page
    // has to ask again until the answer lands. Handing that to React Query
    // instead of a manual setInterval matters on the phone app: its interval
    // pauses while the window is hidden, and it re-renders nothing when the
    // payload is unchanged, where a 3 s `refetch()` rebuilt the whole tool
    // table (and fought the user's scroll) for as long as the check ran.
    refetchInterval: (query) => (query.state.data?.checking ? 5000 : false),
  });
  // What this build can do (GET /api/capabilities). It is the only thing that
  // can tell a tool nobody installed yet from one this device can never run,
  // and the page must not offer the second kind an Install button.
  const { data: caps } = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api.capabilities(),
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const deviceReason = deviceUnavailable(caps);
  const unavailable = unavailableFeatures(caps);
  const refreshNow = () => {
    forceRef.current = true;
    refetch().finally(() => {
      forceRef.current = false;
    });
  };

  const tools: DepTool[] = deps?.tools ?? [];
  // Only what this host can FETCH counts as missing/update for the buttons:
  // `installable` is the backend's own answer (a Windows-only tool on Linux,
  // or one the image already provides as a distro package, has nothing to
  // download — pressing Install on those is what made a working server look
  // broken). They stay in the table below, labelled with the reason.
  const missing = tools.filter((t) => t.state === "missing" && t.installable !== false);
  const updates = tools.filter((t) => t.state === "update" && t.installable !== false);
  const blocked = tools.filter((t) => t.state === "missing" && t.installable === false);
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
      const summary = installSummary(r.results);
      if (r.results.some((x) => !x.ok)) toast.error(summary);
      else toast.success(summary);
      refetch();
    } catch (e) {
      // The error the server sent, verbatim: "sign in required" and "this
      // server has no password yet" are the two this page can hit, and both
      // have an action attached that a rewritten message would hide.
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
            <button className="btn-ghost !py-1 text-xs tap" onClick={refreshNow} disabled={busy || isLoading}>
              <RotateCcw className="h-3 w-3" /> Refresh
            </button>
            {(missing.length > 0 || updates.length > 0) && (
              <button
                className="btn-ghost !py-1 text-xs tap"
                onClick={() => install([...missing, ...updates].map((t) => t.key))}
                disabled={busy || !!deviceReason}
                title={deviceReason ?? undefined}
              >
                Install {missing.length + updates.length} ({missing.length} missing · {updates.length} updates)
              </button>
            )}
            <button
              className="btn-primary !py-1 text-xs tap"
              onClick={() => install()}
              disabled={busy || !!deviceReason}
              title={deviceReason ?? (blocked.length
                ? `Installs what is missing or behind — skips the ${blocked.length} tool(s) this host cannot install`
                : "Installs what is missing or behind; anything already at the newest release is left alone")}
            >
              {busy ? "Installing…" : "Install / update all"}
            </button>
          </>
        }
      />

      {/* One honest line about what THIS build can do, from the backend's own
          capability report. A tool that is missing on a device that can start
          one is a download; a device that cannot start a program at all is
          never fixed by one, and the table below says which of the two it is
          looking at. */}
      {caps && deviceReason && (
        <div className="rounded-md border border-border bg-panel/40 px-3 py-2 text-[11px] text-zinc-400 leading-relaxed">
          <span className="text-amber-400">Unavailable on this device</span> — {deviceReason}. Browsing, tagging,
          playing, playlists and lyrics work here; the {unavailable.length} other feature
          {unavailable.length === 1 ? "" : "s"} need a tool this device can start.
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-500">
        <span>
          {isLoading ? "Checking tools…" : `${ready}/${tools.length} ready`}
          {updates.length > 0 && <span className="text-amber-400"> · {updates.length} update(s) available</span>}
          {missing.length > 0 && <span className="text-red-400"> · {missing.length} missing</span>}
          {/* Not "missing": there is nothing this host could install, so the
              count must not read as work waiting to be done. The row carries
              the reason. */}
          {blocked.length > 0 && (
            <span className="text-zinc-400" title={blocked.map((t) => `${t.name}: ${t.install_note}`).join("\n")}>
              {" "}· {blocked.length} not installable here
            </span>
          )}
          {deps?.checking && <span className="text-zinc-400"> · checking upstream…</span>}
        </span>
        {deps?.deps_dir && (
          <button
            className="inline-flex items-center gap-1 min-w-0 tap hover:text-zinc-300 transition-colors font-mono max-w-[24rem]"
            onClick={openDepsDir}
            title="Open the dependencies folder"
          >
            <FolderOpen className="h-3 w-3 shrink-0" />
            {/* Ellipsis on the child: `truncate` on the flex row itself would
                clip both ends of the path instead of adding an ellipsis. */}
            <span className="truncate min-w-0">{String(deps.deps_dir).replace(/\\/g, "/")}</span>
          </button>
        )}
      </div>

      {noTools && (
        <EmptyState
          title="No tools reported"
          hint="Could not read the tool list — the backend may be down, this client may have been signed out, or the server may still be unclaimed (it answers “finish setup to continue” until a password is set). Refresh once it answers."
        />
      )}

      {/* `table-scroll` alone: the companion `overflow-hidden` utility wins the
          cascade (utilities layer) and clipped the table instead of scrolling it. */}
      {!noTools && (
      <div className="rounded-lg border border-border table-scroll">
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
              {/* The only column that needs a width: its text is a shortened
                  path, and the longest token in it is the version-prefixed file
                  name ("…/AudioAuditor v2.0.0/AudioAuditorCLI.exe", measured
                  153 px at this column's 11 px mono). Six equal columns at the
                  table's 46 rem floor leave it 123 px, which crushes that token;
                  180 px is 153 + the cell's 24 px padding, rounded up. The other
                  five columns share what is left, so from `md` up — where the
                  table is wider than its floor — the layout is as it was. */}
              <th className="th w-[180px]">Location</th>
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
                    <span
                      className={`chip border ${deviceReason || t.installable === false
                        ? "bg-zinc-800 text-zinc-400 border-zinc-700"
                        : "bg-red-900/50 text-red-300 border-red-900"}`}
                      title={deviceReason ?? t.install_note ?? undefined}
                    >
                      {deviceReason
                        ? "Unavailable here"
                        : t.installable === false
                          ? t.install_kind === "system" ? "System package" : "No build here"
                          : "Missing"}
                    </span>
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
        Install downloads from the tool's GitHub releases into the dependencies folder; PATH-installed tools
        (scoop etc.) are shown as ready. A tool that is already installed takes the newest release{" "}
        <span className="text-zinc-500">Available</span> names — that is what the Update chip offers. A first
        install takes the reviewed pinned version instead.
        {deps?.note && <span className="text-amber-500"> Upstream check: {deps.note}</span>}
        {deps?.upstream_checked_at && (
          <span> Checked {new Date(deps.upstream_checked_at).toLocaleTimeString()}.</span>
        )}
      </div>
    </div>
  );
}
