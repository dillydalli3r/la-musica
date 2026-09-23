import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Copy, FolderOpen, Loader2, RotateCcw, Wrench } from "lucide-react";
import { api, deviceUnavailable, installSummary, unavailableFeatures } from "../api";
import { toast } from "../store";
import PageHeader from "../components/PageHeader";
import { EmptyState } from "../components/Badges";
import StorageCard from "../components/StorageCard";

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
  /** True when the copy this row describes was found in the app's PRE-MOVE
   *  tools folder (<app folder>/.dependencies), which is still read so an
   *  install made before the move keeps working. Nothing installs there any
   *  more: a reinstall or update lands under the music folder. */
  legacy_root?: boolean;
  /** What this row's ACTION column offers, decided by the backend so the page
   *  never has to work it out from `state` + `install_kind` itself: `install`
   *  and `update` are downloads (missing / behind upstream), `upgrade` means
   *  the OS package manager owns the tool, `none` means nothing to do here. */
  action?: "install" | "update" | "upgrade" | "none";
  /** The exact upgrade command `upgrade` copies — apt's, for this host. Shown
   *  and copied, never run: this app does not drive a package manager. */
  upgrade_command?: string | null;
};

/** Sidebar "Dependencies" — the external binaries the scripts shell out to
 * (ffmpeg, yt-dlp, beets…). Mirrors the setup wizard's tool check, but always
 * reachable: inspect versions, install into the music folder's .mlo/tools, and
 * pull updates in one click. */
export default function DependenciesPage() {
  const [busy, setBusy] = useState(false);
  // The row whose own Install/Update press is in flight. The page-level button
  // settles the whole table through `busy` + a refetch (the same mechanism the
  // per-row press uses), and this only decides which row shows the spinner.
  const [busyKey, setBusyKey] = useState<string | null>(null);
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
  // Every row that is BEHIND upstream is an update, whichever way it can be
  // installed — a distro row sitting on an older package is still behind, and
  // the header counts the same predicate its amber chip and amber Available
  // value do, so the two can no longer disagree about how many there are.
  const updates = tools.filter((t) => t.state === "update");
  // Only what this host can FETCH is work for a button: `installable` is the
  // backend's own answer (a Windows-only tool on Linux, or one the image
  // already provides as a distro package, has nothing to download — pressing
  // Install on those is what made a working server look broken). They stay in
  // the table above, labelled with the reason and the command that IS theirs.
  const depsUpdates = updates.filter((t) => t.installable !== false);
  const missing = tools.filter((t) => t.state === "missing" && t.installable !== false);
  const blocked = tools.filter((t) => t.state === "missing" && t.installable === false);
  const sysUpdates = updates.filter((t) => t.installable === false);
  const ready = tools.filter((t) => t.state === "ok").length;
  // The tool list is empty only when the payload could not be read at all —
  // that case is the page's empty state, not a one-row table.
  const noTools = !isLoading && tools.length === 0;
  // What the one page-level button would do, in words. It stays on screen (and
  // disabled) when there is nothing it can do, so the reason belongs in its
  // title rather than in its absence.
  const nothingToDo = missing.length === 0 && depsUpdates.length === 0;
  const actionTitle = deviceReason ?? (
    nothingToDo
      ? sysUpdates.length
        ? `Everything this host can install is current — the ${sysUpdates.length} row(s) the system package manager owns are upgraded with the command each row offers`
        : "Everything is current — nothing to install or update"
      : blocked.length
        ? `Installs or updates the ${missing.length + depsUpdates.length} tool(s) this host can install, skipping the ${blocked.length} it cannot`
        : "Installs what is missing or behind; anything already at the newest release is left alone"
  );

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

  /** One install request for the page button (`keys` undefined = everything
   *  missing or behind) or for a single row (`keys` = [that tool]).
   *
   *  `pressed` is the row whose own button asked, so THAT row carries the
   *  spinner; the settling is the same either way — the shared `busy` flag
   *  plus the refetch below, so a row never shows a state the server has not
   *  answered yet. */
  const install = async (keys?: string[], pressed?: string) => {
    setBusy(true);
    setBusyKey(pressed ?? null);
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
      setBusyKey(null);
    }
  };

  /** Hand the row's own upgrade command to the clipboard — the command the
   *  BACKEND built for this host, so the page never spells out a package name
   *  itself. Copied, never run: this app does not drive a package manager. */
  const copyCommand = async (cmd: string) => {
    try {
      await navigator.clipboard.writeText(cmd);
      toast.success(`Copied: ${cmd}`);
    } catch {
      // The Clipboard API needs a secure context, and this app is normally
      // served over plain http on the LAN, where it is undefined — so the
      // command goes in the message instead of being lost with the copy.
      toast.error(`Could not reach the clipboard — run it yourself: ${cmd}`);
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
      {/* `sticky`: the page scrolls through fifteen rows and the action that
          fixes them all must not scroll away with the first ones. The primary
          action lives in this header's actions row, so the whole bar pins
          below the top bar while the table scrolls under it. */}
      <PageHeader
        icon={Wrench}
        title="Dependencies"
        sticky
        subtitle="External tools the scripts rely on. Missing ones are downloaded into the music folder's .mlo/tools — nothing is installed system-wide."
        actions={
          <>
            <button className="btn-ghost !py-1 text-xs tap" onClick={refreshNow} disabled={busy || isLoading}>
              <RotateCcw className="h-3 w-3" /> Refresh
            </button>
            {/* ONE page-level button, and its label says what the press will
                do to the majority of the work waiting: with an update behind
                it, that is "Update all" (a row already installed takes the
                NEWEST release upstream has, not the pin), and it still
                installs the missing tools on that same press. It counts only
                the rows a DOWNLOAD can move (`depsUpdates`): the header's
                update count includes the distro rows too, and those are
                upgraded with the command their own row offers, not with a
                press here. The counts sit in the title and the aria-label,
                and `busy` covers the whole table — a second button for
                "missing only" would have been a subset of this one.

                It is never hidden, and it is never disabled for a count that
                is not about it: the header can say "4 update(s) available"
                while this button is off (all four are the package manager's),
                and the title says which case that is instead of leaving a
                button that looks broken. The header it lives in is `sticky`,
                so it stays on screen while the fifteen rows scroll. */}
            <button
              className="btn-primary !py-1 text-xs tap"
              onClick={() => install()}
              disabled={busy || !!deviceReason || nothingToDo}
              title={actionTitle}
              aria-label={`${depsUpdates.length ? "Update all" : "Install all"}: ${missing.length} missing, ${depsUpdates.length} update(s) this host can install`}
            >
              {busy && !busyKey ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
              {busy && !busyKey ? "Installing…" : depsUpdates.length ? "Update all" : "Install all"}
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

      <StorageCard />

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-500">
        <span>
          {isLoading ? "Checking tools…" : `${ready}/${tools.length} ready`}
          {/* Every row that is behind upstream, the distro-owned ones
              included: `updates` is the same filter the amber chip and the
              amber Available value are drawn from, so the count can no longer
              be smaller than the number of amber cells on screen. The title
              names them, plus the command a distro row needs instead of a
              button here. */}
          {updates.length > 0 && (
            <span
              className="text-amber-400"
              title={updates.map((t) => `${t.name}: installed ${t.installed_version ?? "?"}, available ${t.upstream_version ?? "?"}${t.upgrade_command ? ` — ${t.upgrade_command}` : ""}`).join("\n")}
            >
              {" "}· {updates.length} update(s) available
            </span>
          )}
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
              <th className="th" title="The version this app installs for a FIRST install — on Linux a distro-provided tool shows its system package instead">
                Latest
              </th>
              <th className="th" title="Newest release published upstream on GitHub. An installed tool takes that one when you press Update — the reviewed pin in Latest is what a FIRST install fetches.">
                Available
              </th>
              {/* The only column that needs a width: its text is a shortened
                  path, and the longest token in it is the version-prefixed file
                  name ("…/AudioAuditor v2.0.0/AudioAuditorCLI.exe", measured
                  153 px at this column's 11 px mono). Six equal columns at the
                  table's 46 rem floor left it 123 px, which crushes that token;
                  180 px is 153 + the cell's 24 px padding, rounded up. It stays
                  fixed as the Action column takes its width out of the flexible
                  ones instead — a button is a short label, a path is not. */}
              <th className="th w-[180px]">Location</th>
              <th className="th">Action</th>
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
                      /* `cell-nowrap`: the cell's own `word-break` is happy to
                         break this status word, and at phone width the ellipsis
                         of "Checking…" wrapped to a line of its own (the cell
                         has room for neither the label nor the version it
                         stands for). A one-word status is one line. */
                      ? <span className="cell-nowrap text-zinc-600 italic">Checking…</span>
                      : <span className="cell-nowrap text-zinc-600 italic">Unknown</span>}
                </td>
                <td className="td text-[11px] text-zinc-600 font-mono truncate"
                    title={t.legacy_root
                      ? `${t.path ?? ""} — in the app's old tools folder; a reinstall or update lands under the music folder`
                      : (t.path ?? "")}>
                  {t.path ? shortPath(t.path) : "—"}
                </td>
                {/* One row's own action, from the backend's `action` rather
                    than from the state + kind combination the page would
                    otherwise have to re-derive: `install`/`update` press THIS
                    row's install, `upgrade` copies the command the package
                    manager needs (this app never runs one), and `none` means
                    the row has nothing to do here — its chip and note say why.
                    The row's own note is the hover text, so a button can never
                    promise more than the row it belongs to.

                    `upgrade` is the ONLY case with a command to copy: it is
                    the backend's word for "the OS package manager owns this
                    one", so a row this app can download — every Linux row now,
                    libjpeg-turbo and libjxl included — never offers a command
                    instead of a button. A row with no install path AND no
                    command (`unsupported`) shows nothing here; its chip and
                    note are the whole answer. */}
                <td className="td">
                  {t.action === "install" || t.action === "update" ? (
                    <button
                      className="btn-ghost !py-0.5 text-[11px] tap"
                      onClick={() => install([t.key], t.key)}
                      disabled={busy || !!deviceReason}
                      title={
                        deviceReason ??
                        (t.action === "update"
                          ? t.note ?? (t.upstream_version ? `Upstream: ${t.upstream_version}` : undefined)
                          : t.install_note ?? t.note ?? undefined)
                      }
                    >
                      {busyKey === t.key ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : null}
                      {t.action === "update" ? "Update" : "Install"}
                    </button>
                  ) : t.action === "upgrade" && t.upgrade_command ? (
                    <button
                      className="btn-ghost !py-0.5 text-[11px] tap"
                      onClick={() => copyCommand(t.upgrade_command!)}
                      title={t.note ?? t.upgrade_command}
                    >
                      <Copy className="h-3 w-3" /> Copy command
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}

      <div className="text-[10px] text-zinc-600">
        Install downloads from the tool's GitHub releases into the music folder's .mlo/tools; PATH-installed tools
        (scoop etc.) are shown as ready. Tools installed before that move are still found in the app's old
        .dependencies folder — the row's own note says so, and a reinstall puts them under the music folder. An INSTALLED tool takes the newest release{" "}
        <span className="text-zinc-500">Available</span> names — that is what the Update chip and its button
        offer — and a row already at that version is a no-op (nothing is downloaded). Only a FIRST install
        takes the reviewed pinned version in <span className="text-zinc-500">Latest</span>. A row the OS package
        manager owns (a distro tool) is never downloaded over: it shows{" "}
        <span className="text-zinc-500">Copy command</span> with the exact upgrade command for this host, which
        you run yourself — this app never touches a package manager.
        {deps?.note && <span className="text-amber-500"> Upstream check: {deps.note}</span>}
        {deps?.upstream_checked_at && (
          <span> Checked {new Date(deps.upstream_checked_at).toLocaleTimeString()}.</span>
        )}
      </div>
    </div>
  );
}
