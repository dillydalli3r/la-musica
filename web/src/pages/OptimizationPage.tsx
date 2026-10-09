import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check, ChevronDown as Down, ChevronUp as Up, Gauge, Play, RefreshCw, Wand2, X,
} from "lucide-react";
import { api } from "../api";
import { toast, useStore } from "../store";
import { ProgressInline } from "../components/ProgressBar";
import PageHeader from "../components/PageHeader";
import { ForceControl, useForceRun } from "../components/ForceRun";
import { FORCE_SCRIPTS, forceDict } from "../lib/force";
import { SCRIPTS, DEFAULT_RUN_ALL, isScriptId } from "../lib/scripts";
import type { ScriptRunResult } from "../types";

// Selected scripts + their custom run order, persisted across reloads.
const SEL_KEY = "mlo.opt.sel.v1";

function loadSel(): number[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SEL_KEY) || "[]");
    if (Array.isArray(raw)) {
      return raw.filter((n) => SCRIPTS.some((s) => s.ids[0] === n));
    }
  } catch {
    /* fall through */
  }
  return [];
}

/** Optimization hub: Run All (+ Force), per-script runs and a library
 * refresh — everything that used to live in the top bar, in one place. */
export default function OptimizationPage() {
  const qc = useQueryClient();
  const { progress } = useStore();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const { forceRun, toggle: toggleForce, forceSel, setSel } = useForceRun();
  const [busy, setBusy] = useState(false);
  // The last run's own per-script report, on the page. A run that failed some
  // of its scripts used to say "see console" and leave it there — the one
  // surface that showed which script failed and why was a place the user
  // cannot open.
  const [run, setRun] = useState<{ label: string; results: ScriptRunResult[] } | null>(null);

  const runAll = async () => {
    setBusy(true);
    toast(forceRun
      ? `Running all scripts (forced: ${FORCE_SCRIPTS.filter((f) => forceSel[f.key]).length}/${FORCE_SCRIPTS.length})…`
      : "Running all scripts…");
    try {
      const order = Array.isArray(config?.run_all_order) && (config!.run_all_order as number[]).length
        ? (config!.run_all_order as number[]).filter(isScriptId)
        : DEFAULT_RUN_ALL;
      const force = forceRun ? forceDict(forceSel) : undefined;
      const res = await api.run(order, undefined, force);
      const results = res.results ?? [];
      setRun({ label: forceRun ? "Run All (forced)" : "Run All", results });
      const failed = results.filter((r) => r.error);
      if (failed.length) toast.error(`${failed.length} of ${results.length} script(s) failed — the run's results are below`);
      else toast.success("Run All finished");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      qc.invalidateQueries({ queryKey: ["library"] });
    }
  };

  // ---- multi-select with a custom run order (persisted) --------------------
  const [sel, setSelIds] = useState<number[]>(loadSel);
  const persistSel = (v: number[]) => {
    setSelIds(v);
    try {
      localStorage.setItem(SEL_KEY, JSON.stringify(v));
    } catch {
      /* ignore */
    }
  };
  const toggleSel = (id: number) =>
    persistSel(sel.includes(id) ? sel.filter((x) => x !== id) : [...sel, id]);
  const move = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= sel.length) return;
    const v = [...sel];
    [v[i], v[j]] = [v[j], v[i]];
    persistSel(v);
  };
  const runSelected = async () => {
    if (!sel.length) return;
    const labels = sel.map((id) => SCRIPTS.find((s) => s.ids[0] === id)?.label ?? `#${id}`);
    await runScripts(sel, `Selected (${sel.length}): ${labels.join(" → ")}`);
  };
  const selLabel = (id: number) => SCRIPTS.find((s) => s.ids[0] === id)?.label ?? `#${id}`;

  const runScripts = async (ids: number[], label: string) => {
    setBusy(true);
    const force = forceRun ? forceDict(forceSel) : undefined;
    toast(`Running ${label}${force ? " (forced)" : ""}…`);
    try {
      const res = await api.run(ids, undefined, force);
      const results = res.results ?? [];
      setRun({ label, results });
      const failed = results.filter((r) => r.error);
      if (failed.length) toast.error(`${failed.length} of ${results.length} script(s) failed — the run's results are below`);
      else toast.success(`${label} finished`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      qc.invalidateQueries({ queryKey: ["library"] });
    }
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <PageHeader
        icon={Gauge}
        title="Optimization"
        subtitle="Run the library maintenance scripts — individually, as a custom selection, or all together in the order that is best for file optimization. Force re-runs scripts that would normally skip because they are already done."
      />

      {/* ---- Run All + Force ------------------------------------------ */}
      <div className="panel">
        <div className="flex items-center gap-2 flex-wrap">
          <button
            className="btn-primary text-xs tap"
            disabled={busy}
            onClick={runAll}
            title="Runs every script in the best order for file optimization: remux first (bit-exact video pass), then tags/lyrics, analysis, audio work — grading always last"
          >
            <Play className="h-3.5 w-3.5" /> Run All
          </button>
          <ForceControl forceRun={forceRun} toggle={toggleForce} forceSel={forceSel} setSel={setSel} />
          <button
            className="btn-ghost text-xs tap"
            disabled={busy}
            onClick={() => qc.invalidateQueries({ queryKey: ["library"] })}
          >
            <RefreshCw className="h-3.5 w-3.5" /> Refresh library
          </button>
          {busy && <span className="text-[11px] text-zinc-500">running…</span>}
        </div>
        {/* Live progress while a script / export runs */}
        <div className="mt-2 min-h-[20px]">{progress && <ProgressInline progress={progress} />}</div>
      </div>

      {/* ---- the last run's own results ---------------------------------
          Every script the run reported, with what it said when it failed —
          the summary a "see console" toast never was. It stays until it is
          dismissed or the next run replaces it, so a run that outlived its
          toast still has an answer on the page. */}
      {run && (
        <div className="panel">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="text-xs font-bold text-zinc-300 flex items-center gap-1.5 flex-1 min-w-0">
              <Check className="h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 break-words">{run.label} — results</span>
            </div>
            <span className="text-[11px] text-zinc-500">
              {run.results.filter((r) => !r.error && !r.skipped).length} ran ·{" "}
              {run.results.filter((r) => r.error).length} failed ·{" "}
              {run.results.filter((r) => r.skipped && !r.error).length} skipped
            </span>
            <button className="btn-ghost !py-1 text-[11px] tap" onClick={() => setRun(null)}>
              <X className="h-3 w-3" /> Dismiss
            </button>
          </div>
          {run.results.length === 0 ? (
            <div className="mt-2 text-[11px] text-zinc-500">
              The run reported no per-script results.
            </div>
          ) : (
            <div className="mt-2 stagger divide-y divide-border/60 border border-border rounded-lg overflow-hidden">
              {run.results.map((r) => {
                const label = r.label ?? r.name ?? SCRIPTS.find((s) => s.ids[0] === r.id)?.label ?? `#${r.id}`;
                return (
                  <div key={r.id} className="px-2.5 py-1.5 text-[11px] flex items-start gap-2">
                    <span
                      className={`shrink-0 font-mono ${
                        r.error ? "text-amber-200" : r.skipped ? "text-zinc-500" : "text-emerald-300"
                      }`}
                    >
                      {r.error ? "failed" : r.skipped ? "skipped" : "ok"}
                    </span>
                    <span className="min-w-0 break-words text-zinc-300">
                      {label}
                      {(r.error ?? r.reason) && <span className="text-zinc-500"> — {r.error ?? r.reason}</span>}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* ---- individual scripts + custom multi-select run ------------- */}
      <div className="panel">
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <div className="text-xs font-bold text-zinc-300 flex items-center gap-1.5 flex-1 min-w-0">
            <Wand2 className="h-3.5 w-3.5" /> Individual scripts
          </div>
          {sel.length > 0 && (
            <>
              <span className="text-[11px] text-zinc-500">{sel.length} selected</span>
              <button className="btn-ghost !py-1 text-[11px] tap" disabled={busy} onClick={() => persistSel([])}>
                Clear
              </button>
            </>
          )}
          <button
            className="btn-primary !py-1 text-xs tap"
            disabled={busy || !sel.length}
            onClick={runSelected}
            title={sel.length ? `Run in order: ${sel.map(selLabel).join(" → ")}` : "Tick scripts below to build a custom run"}
          >
            <Play className="h-3.5 w-3.5" /> Run selected{sel.length ? ` (${sel.length})` : ""}
          </button>
        </div>
        <div className="stagger grid sm:grid-cols-2 gap-1.5">
          {SCRIPTS.map((s) => {
            const id = s.ids[0];
            const on = sel.includes(id);
            const orderIdx = sel.indexOf(id) + 1;
            return (
              <div
                key={id}
                className={`flex items-center gap-2 rounded-lg border px-2 py-1.5 text-xs ${
                  on ? "border-accent/50 bg-accent/10" : "border-border"
                }`}
              >
                <input
                  type="checkbox"
                  checked={on}
                  disabled={busy}
                  onChange={() => toggleSel(id)}
                  title="Include in the custom run"
                />
                <span className="flex-1 min-w-0 truncate text-zinc-300" title={s.label}>
                  {s.label}
                </span>
                {on && (
                  <span
                    className="h-4.5 min-w-[18px] px-1 rounded bg-accent on-accent text-[10px] font-mono flex items-center justify-center"
                    title={`Position ${orderIdx} in the custom run`}
                  >
                    {orderIdx}
                  </span>
                )}
                <button
                  className="btn-ghost !p-1 tap min-w-11 md:min-w-0 shrink-0"
                  disabled={busy}
                  onClick={() => runScripts(s.ids, s.label)}
                  title={`Run: ${s.label}`}
                >
                  <Play className="h-3 w-3" />
                </button>
              </div>
            );
          })}
        </div>
        {sel.length > 1 && (
          <div className="mt-3 pt-3 border-t border-border">
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">Custom run order</div>
            <div className="stagger flex flex-col gap-1">
              {sel.map((id, i) => (
                <div key={id} className="flex items-center gap-2 text-xs text-zinc-300 bg-panel rounded-lg px-2 py-1">
                  <span className="font-mono text-[10px] text-zinc-500 w-4 text-right">{i + 1}</span>
                  <span className="flex-1 min-w-0 truncate">{selLabel(id)}</span>
                  <button className="btn-ghost !px-1 !py-0.5 tap min-w-11 md:min-w-0" disabled={i === 0 || busy} onClick={() => move(i, -1)} title="Move up">
                    <Up className="h-3 w-3" />
                  </button>
                  <button className="btn-ghost !px-1 !py-0.5 tap min-w-11 md:min-w-0" disabled={i === sel.length - 1 || busy} onClick={() => move(i, 1)} title="Move down">
                    <Down className="h-3 w-3" />
                  </button>
                  <button className="btn-ghost !px-1 !py-0.5 tap min-w-11 md:min-w-0" disabled={busy} onClick={() => toggleSel(id)} title="Remove from the custom run">
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="text-[10px] text-zinc-600 mt-2">
          Tick scripts to build a custom run (use the arrows to set the order), or run any script alone with its play button.
          To run scripts on a specific album or track selection, use the selection menu in the library view — those runs only
          touch what you selected.
        </div>
      </div>

    </div>
  );
}
