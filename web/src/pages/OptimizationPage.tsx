import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check, ChevronDown, ChevronDown as Down, ChevronUp as Up, Gauge, Play, RefreshCw, Wand2, X, Zap,
} from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import { ProgressInline } from "../components/ProgressBar";
import { FORCE_SCRIPTS, forceDict, loadForceSel, saveForceSel } from "../lib/force";
import { SCRIPTS, DEFAULT_RUN_ALL, isScriptId } from "../lib/scripts";

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

/** One-shot force state (toggle + per-script selection) shared by Run All.
 * Extracted from the old header so any surface reuses the same behavior. */
function useForceRun() {
  const [forceRun, setForceRun] = useState(() => localStorage.getItem("mlo.runAll.force") === "1");
  const [forceSel, setForceSel] = useState<Record<string, boolean>>(loadForceSel);
  const toggle = () => {
    const v = !forceRun;
    setForceRun(v);
    localStorage.setItem("mlo.runAll.force", v ? "1" : "0");
  };
  const setSel = (next: Record<string, boolean>) => {
    setForceSel(next);
    saveForceSel(next);
  };
  return { forceRun, toggle, forceSel, setSel };
}

/** Optimization hub: Run All (+ Force), per-script runs and a library
 * refresh — everything that used to live in the top bar, in one place. */
export default function OptimizationPage() {
  const qc = useQueryClient();
  const { setToast, progress } = useStore();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const { forceRun, toggle: toggleForce, forceSel, setSel } = useForceRun();
  const [forceMenu, setForceMenu] = useState(false);
  const [busy, setBusy] = useState(false);

  const runAll = async () => {
    setBusy(true);
    setToast(forceRun
      ? `Running all scripts (forced: ${FORCE_SCRIPTS.filter((f) => forceSel[f.key]).length}/${FORCE_SCRIPTS.length})…`
      : "Running all scripts…");
    try {
      const order = Array.isArray(config?.run_all_order) && (config!.run_all_order as number[]).length
        ? (config!.run_all_order as number[]).filter(isScriptId)
        : DEFAULT_RUN_ALL;
      const force = forceRun ? forceDict(forceSel) : undefined;
      const res = await api.run(order, undefined, force);
      const failed = (res.results ?? []).filter((r) => r.error);
      setToast(failed.length ? `${failed.length} script(s) failed — see console` : "Run All finished");
    } catch (e) {
      setToast(String(e));
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

  const runScripts = async (ids: number[], label: string, force?: boolean) => {
    setBusy(true);
    setToast(`Running ${label}${force ? " (forced)" : ""}…`);
    try {
      const res = await api.run(ids, undefined, force ? forceDict(forceSel) : undefined);
      const failed = (res.results ?? []).filter((r) => r.error);
      setToast(failed.length ? `${label} failed: ${failed[0].error}` : `${label} finished`);
    } catch (e) {
      setToast(String(e));
    } finally {
      setBusy(false);
      qc.invalidateQueries({ queryKey: ["library"] });
    }
  };

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
        <Gauge className="h-6 w-6" /> Optimization
      </h1>
      <p className="text-xs text-zinc-500 mt-1">
        Run the library maintenance scripts — individually, as a custom selection, or all together in
        the order that is best for file optimization. Force re-runs scripts that would normally skip
        because they are already done.
      </p>

      {/* ---- Run All + Force ------------------------------------------ */}
      <div className="bg-card rounded-lg border border-border p-4 mt-5">
        <div className="flex items-center gap-2 flex-wrap">
          <button
            className="btn-primary text-xs"
            disabled={busy}
            onClick={runAll}
            title="Runs every script in the best order for file optimization: remux first (bit-exact video pass), then tags/lyrics, analysis, audio work — grading always last"
          >
            <Play className="h-3.5 w-3.5" /> Run All
          </button>
          <div className="relative flex items-center">
            <button
              className={`btn-ghost text-xs rounded-r-none border-r-0 ${forceRun ? "!text-accent border border-accent/50" : ""}`}
              onClick={toggleForce}
              title="Force the selected scripts on the next runs — ignores their 'already done' skips (one-shot, saved Settings are untouched)"
            >
              <Zap className={`h-3.5 w-3.5 ${forceRun ? "fill-current" : ""}`} /> Force
              {forceRun && (
                <span className="ml-1 text-[10px] font-mono opacity-80">
                  {FORCE_SCRIPTS.filter((f) => forceSel[f.key]).length}/{FORCE_SCRIPTS.length}
                </span>
              )}
            </button>
            <button
              className={`btn-ghost text-xs rounded-l-none !px-1 ${forceRun ? "!text-accent" : ""}`}
              onClick={() => setForceMenu(!forceMenu)}
              title="Choose which scripts are forced"
            >
              <ChevronDown className="h-3.5 w-3.5" />
            </button>
            {forceMenu && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setForceMenu(false)} />
                <div className="absolute left-0 top-full mt-1 z-50 bg-zinc-950 border border-border rounded-lg p-1.5 w-60 shadow-2xl">
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-1 pb-1.5">
                    Force when Force is on
                  </div>
                  {FORCE_SCRIPTS.map((f) => (
                    <label key={f.key} className="flex items-center gap-2 px-1 py-1 rounded text-xs text-zinc-300 hover:bg-panel cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={forceSel[f.key] !== false}
                        onChange={(e) => setSel({ ...forceSel, [f.key]: e.target.checked })}
                      />
                      {f.label}
                    </label>
                  ))}
                  <div className="flex items-center gap-1.5 pt-1.5 mt-1 border-t border-border">
                    <button
                      className="btn-ghost !py-1 text-[11px] flex-1 inline-flex items-center justify-center gap-1"
                      onClick={() => setSel(Object.fromEntries(FORCE_SCRIPTS.map((f) => [f.key, true])))}
                    >
                      <Check className="h-3 w-3" /> All
                    </button>
                    <button
                      className="btn-ghost !py-1 text-[11px] flex-1"
                      onClick={() => setSel(Object.fromEntries(FORCE_SCRIPTS.map((f) => [f.key, false])))}
                    >
                      None
                    </button>
                  </div>
                  <div className="text-[10px] text-zinc-600 px-1 pt-1">
                    Force is a one-shot switch — saved Settings are not changed.
                  </div>
                </div>
              </>
            )}
          </div>
          <button
            className="btn-ghost text-xs"
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

      {/* ---- individual scripts + custom multi-select run ------------- */}
      <div className="bg-card rounded-lg border border-border p-4 mt-4">
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <div className="text-xs font-bold text-zinc-300 flex items-center gap-1.5 flex-1 min-w-0">
            <Wand2 className="h-3.5 w-3.5" /> Individual scripts
          </div>
          {sel.length > 0 && (
            <>
              <span className="text-[11px] text-zinc-500">{sel.length} selected</span>
              <button className="btn-ghost !py-1 text-[11px]" disabled={busy} onClick={() => persistSel([])}>
                Clear
              </button>
            </>
          )}
          <button
            className="btn-primary !py-1 text-xs"
            disabled={busy || !sel.length}
            onClick={runSelected}
            title={sel.length ? `Run in order: ${sel.map(selLabel).join(" → ")}` : "Tick scripts below to build a custom run"}
          >
            <Play className="h-3.5 w-3.5" /> Run selected{sel.length ? ` (${sel.length})` : ""}
          </button>
        </div>
        <div className="grid sm:grid-cols-2 gap-1.5">
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
                  className="btn-ghost !p-1 shrink-0"
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
            <div className="flex flex-col gap-1">
              {sel.map((id, i) => (
                <div key={id} className="flex items-center gap-2 text-xs text-zinc-300 bg-panel rounded-lg px-2 py-1">
                  <span className="font-mono text-[10px] text-zinc-500 w-4 text-right">{i + 1}</span>
                  <span className="flex-1 min-w-0 truncate">{selLabel(id)}</span>
                  <button className="btn-ghost !px-1 !py-0.5" disabled={i === 0 || busy} onClick={() => move(i, -1)} title="Move up">
                    <Up className="h-3 w-3" />
                  </button>
                  <button className="btn-ghost !px-1 !py-0.5" disabled={i === sel.length - 1 || busy} onClick={() => move(i, 1)} title="Move down">
                    <Down className="h-3 w-3" />
                  </button>
                  <button className="btn-ghost !px-1 !py-0.5" disabled={busy} onClick={() => toggleSel(id)} title="Remove from the custom run">
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
