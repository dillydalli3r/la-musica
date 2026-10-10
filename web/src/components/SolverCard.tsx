import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Play, Square, Download } from "lucide-react";
import { api, type SolverStatus } from "../api";
import { toast } from "../store";

/** The Cloudflare solver, in one card (Settings → Discovery).
 *
 *  Both scraped sources (RateYourMusic, Album of the Year) sit behind
 *  Cloudflare's challenge. A cleared session cookie used to be the only way
 *  past it, and a cookie is a chore: `cf_clearance` is bound to one browser's
 *  User-Agent and network, it expires, and a browser Cloudflare never
 *  challenged has none to export at all. This card is the alternative — install
 *  FlareSolverr once, press Start, and the app drives a real browser through
 *  the challenge itself (server/solver.py owns the process; the wire format is
 *  the same `cf_solver_url` contract it already spoke).
 *
 *  Everything the card shows comes from `GET /api/solver`, including WHERE the
 *  effective URL came from — the container's `MLO_SOLVER_URL`, the child
 *  process this app runs, or a solver already listening. No URL is ever typed
 *  in for the normal case: installing FlareSolverr IS the setup, and the only
 *  field here is the deliberate override for a solver this machine does not
 *  run (another host, another port), kept out of the way in a disclosure.
 *
 *  Install reuses the Dependencies installer (`/api/dependencies/install`) —
 *  the download is ~300 MB and takes minutes, which is why the button says so
 *  — rather than keeping a second copy of the fetch/verify/report path here.
 *  Installing also STARTS it (server/main.py), so the card follows the same
 *  poll it uses while the app brings the solver up at boot. */
export default function SolverCard() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<"" | "install" | "start" | "stop" | "cfg">("");
  const [url, setUrl] = useState<string | null>(null);
  const { data, isLoading } = useQuery({
    queryKey: ["solver"],
    queryFn: async () => (await api.solver()).status,
    // The autostart thread may be bringing it up as the card mounts; a couple
    // of polls settle that without a manual refresh.
    refetchInterval: (q) => (q.state.data?.installed && !q.state.data?.url ? 3000 : false),
  });

  const status: SolverStatus | undefined = data;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["solver"] });
  };

  const run = async (what: "start" | "stop", fn: () => Promise<{ ok: boolean; message: string }>) => {
    setBusy(what);
    try {
      const r = await fn();
      if (r.ok) toast.success(r.message || "done");
      else toast.error(r.message || "no change");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy("");
      refresh();
    }
  };

  const install = async () => {
    setBusy("install");
    try {
      const r = await api.installDependencies(["flaresolverr"]);
      const row = r.results?.[0];
      if (row?.ok) {
        toast.success(row.changed === false
          ? `FlareSolverr already at v${row.version}`
          : `FlareSolverr v${row.version} installed`);
      } else {
        toast.error(row?.error || "install failed");
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy("");
      refresh();
      qc.invalidateQueries({ queryKey: ["dependencies"] });
    }
  };

  const setAutostart = async (enabled: boolean) => {
    setBusy("cfg");
    try {
      const r = await api.solverConfig({ enabled });
      if (!r.ok) toast.error(r.message || "could not save");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy("");
      refresh();
    }
  };

  const saveUrl = async (value: string) => {
    setBusy("cfg");
    try {
      const r = await api.solverConfig({ url: value.trim() });
      if (r.ok) {
        toast.success(value.trim()
          ? `Using the solver at ${value.trim()}`
          : "Override cleared — the app finds its own solver again");
        setUrl(null);
      } else {
        toast.error(r.message || "could not save");
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy("");
      refresh();
    }
  };

  const sourceLabel = (s: SolverStatus): string => {
    switch (s.source) {
      case "config": return "from the override below";
      case "env": return "from the MLO_SOLVER_URL environment (the container's solver service)";
      case "managed": return "started by la musica";
      case "local": return "already answering on the default port (a sidecar, or one left running)";
      default: return "";
    }
  };

  return (
    <div className="panel space-y-2">
      <div className="flex items-center gap-2">
        <div className="text-xs font-bold text-zinc-300">Cloudflare solver (FlareSolverr)</div>
        {status?.running && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400">
            running
          </span>
        )}
      </div>
      <div className="text-[10px] text-zinc-600">
        RateYourMusic and Album of the Year both sit behind Cloudflare. Instead of pasting a session
        cookie that expires, install FlareSolverr once and let it pass the challenge with a real
        browser — no cookie to fetch, no URL to fill in, and nothing to re-paste when it stops
        working. Installing it runs it.
      </div>

      {isLoading && (
        <div className="text-[11px] text-zinc-500 flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin" /> checking…
        </div>
      )}

      {status && (
        <>
          <div className="text-[11px] text-zinc-400">
            {status.installed
              ? <>Installed{status.version ? ` (v${status.version})` : ""}.</>
              : <>Not installed on this host.</>}
            {status.url
              ? <> The solver in use: <span className="font-mono text-zinc-300">{status.url}</span> — {sourceLabel(status)}.</>
              : <> No solver is answering, so RateYourMusic and Album of the Year still need their cookies (or they report the challenge).</>}
          </div>

          {status.installed && status.autostart && !status.running
           && status.source === "" && (
            <div className="text-[10px] text-amber-500 flex items-center gap-2">
              <Loader2 className="w-3 h-3 animate-spin" /> starting it…
            </div>
          )}

          {/* Running is not the same as WORKING. The solver can be up and still
              lose a site's challenge (measured live: it cannot pass either
              scraped host from some networks), and the import then skips that
              source — so the card says which hosts it could not pass, in the
              solver's own words, instead of leaving "running" to imply
              everything was fine. */}
          {status.running && status.blocked?.length > 0 && (
            <div className="text-[10px] text-amber-500 space-y-0.5">
              {status.blocked.map((b) => (
                <div key={b.host}>
                  It could not pass {b.label}'s Cloudflare challenge ({b.message}) — that
                  source is skipped and reported as such; a cleared cookie is the other route.
                </div>
              ))}
            </div>
          )}

          {!status.installed && status.install_note && (
            <div className="text-[10px] text-amber-500">{status.install_note}</div>
          )}

          <div className="flex items-center gap-2 flex-wrap pt-1">
            {!status.installed && status.install_kind === "deps" && (
              <button
                className="btn-primary !py-1 text-xs min-h-10 md:min-h-0 tap"
                onClick={install}
                disabled={busy !== ""}
              >
                {busy === "install"
                  ? <><Loader2 className="w-3 h-3 animate-spin inline mr-1" />installing… (~300 MB)</>
                  : <><Download className="w-3 h-3 inline mr-1" />Install FlareSolverr</>}
              </button>
            )}
            {status.installed && status.install_kind === "deps" && (
              <button
                className="btn-ghost !py-1 text-xs tap"
                onClick={install}
                disabled={busy !== ""}
                title="Re-download the pinned release"
              >
                {busy === "install" ? "installing…" : "Reinstall / update"}
              </button>
            )}
            {status.installed && status.source !== "managed" && (
              <button
                className="btn-primary !py-1 text-xs min-h-10 md:min-h-0 tap"
                onClick={() => run("start", api.solverStart)}
                disabled={busy !== "" || status.running}
              >
                {busy === "start"
                  ? <><Loader2 className="w-3 h-3 animate-spin inline mr-1" />starting…</>
                  : <><Play className="w-3 h-3 inline mr-1" />Start</>}
              </button>
            )}
            {status.managed && (
              <button
                className="btn-ghost !py-1 text-xs tap"
                onClick={() => run("stop", api.solverStop)}
                disabled={busy !== ""}
              >
                {busy === "stop"
                  ? <><Loader2 className="w-3 h-3 animate-spin inline mr-1" />stopping…</>
                  : <><Square className="w-3 h-3 inline mr-1" />Stop</>}
              </button>
            )}
          </div>

          {status.installed && (
            <label className="flex items-center gap-2 text-[11px] text-zinc-400">
              <input
                type="checkbox"
                checked={status.autostart}
                disabled={busy !== ""}
                onChange={(e) => setAutostart(e.target.checked)}
              />
              Start it with the app (recommended — otherwise it runs only when you press Start)
            </label>
          )}

          <div className="text-[10px] text-zinc-600">
            The check gives it up to {Math.round(status.timeout_ms / 1000)}s per page (see the
            timeout setting above).{" "}
            {status.log ? <>Its log is <span className="font-mono">{status.log}</span>.</> : null}
          </div>

          {/* The one case the automatic resolution cannot cover. Kept in a
              disclosure on purpose: a plain field here is what made the URL
              look like a required setting. */}
          <details className="text-[10px] text-zinc-500">
            <summary className="cursor-pointer">
              Using a solver somewhere else? (advanced)
            </summary>
            <div className="mt-1 flex items-center gap-2 flex-wrap">
              <input
                className="input flex-1 min-w-[16rem] !py-1 text-[11px] font-mono"
                placeholder="http://host:8191 — empty = the app's own"
                defaultValue={status.source === "config" ? status.url : ""}
                onChange={(e) => setUrl(e.target.value)}
                disabled={busy !== ""}
              />
              <button
                className="btn-ghost !py-1 text-xs tap"
                disabled={busy !== "" || url === null}
                onClick={() => saveUrl(url ?? "")}
              >
                {busy === "cfg" ? "saving…" : "Save"}
              </button>
            </div>
            <div className="mt-1 text-[10px] text-zinc-600">
              Only for a FlareSolverr on another machine or another port. Leave it empty and the app
              uses the solver it runs itself{status.source === "env"
                ? " (here: the MLO_SOLVER_URL environment)" : ""}.
            </div>
          </details>
        </>
      )}
    </div>
  );
}