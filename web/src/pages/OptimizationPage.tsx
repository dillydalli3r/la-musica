import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check, ChevronDown, ChevronDown as Down, ChevronUp as Up, FolderTree, Gauge, Play, RefreshCw, Wand2, X, Zap,
} from "lucide-react";
import { api } from "../api";
import { toast, useStore } from "../store";
import { ProgressInline } from "../components/ProgressBar";
import PageHeader from "../components/PageHeader";
import Popover from "../components/Popover";
import { FORCE_SCRIPTS, forceDict, loadForceSel, saveForceSel } from "../lib/force";
import { SCRIPTS, DEFAULT_RUN_ALL, isScriptId } from "../lib/scripts";
import type { LayoutIssue, LayoutReport } from "../types";

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
  const { progress } = useStore();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const { forceRun, toggle: toggleForce, forceSel, setSel } = useForceRun();
  const [forceMenu, setForceMenu] = useState(false);
  const [busy, setBusy] = useState(false);

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
      const failed = (res.results ?? []).filter((r) => r.error);
      if (failed.length) toast.error(`${failed.length} script(s) failed — see console`);
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
      const failed = (res.results ?? []).filter((r) => r.error);
      if (failed.length) toast.error(`${label} failed: ${failed[0].error}`);
      else toast.success(`${label} finished`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      qc.invalidateQueries({ queryKey: ["library"] });
    }
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
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
          <div className="relative flex items-center">
            <button
              className={`btn-ghost text-xs tap rounded-r-none border-r-0 ${forceRun ? "!text-accent border border-accent/50" : ""}`}
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
              className={`btn-ghost text-xs tap min-w-11 md:min-w-0 rounded-l-none !px-1 ${forceRun ? "!text-accent" : ""}`}
              onClick={() => setForceMenu(!forceMenu)}
              title="Choose which scripts are forced"
            >
              <ChevronDown className="h-3.5 w-3.5" />
            </button>
            <Popover open={forceMenu} onClose={() => setForceMenu(false)} align="left" panelClass="w-60 p-1.5">
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
                      className="btn-ghost !py-1 text-[11px] tap flex-1 inline-flex items-center justify-center gap-1"
                      onClick={() => setSel(Object.fromEntries(FORCE_SCRIPTS.map((f) => [f.key, true])))}
                    >
                      <Check className="h-3 w-3" /> All
                    </button>
                    <button
                      className="btn-ghost !py-1 text-[11px] tap flex-1"
                      onClick={() => setSel(Object.fromEntries(FORCE_SCRIPTS.map((f) => [f.key, false])))}
                    >
                      None
                    </button>
                  </div>
                  <div className="text-[10px] text-zinc-600 px-1 pt-1">
                    Force is a one-shot switch — saved Settings are not changed. It applies to every run
                    started here: Run All, Run selected and the single-script buttons.
                  </div>
            </Popover>
          </div>
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

      <LayoutPanel />
    </div>
  );
}

/** Readable names for the layout scanner's issue kinds, in report order. */
const LAYOUT_KINDS: { kind: string; label: string; bad: boolean }[] = [
  { kind: "audio_at_root", label: "Audio loose in the music folder root", bad: true },
  { kind: "audio_in_artists", label: "Audio loose in Artists/", bad: true },
  { kind: "audio_in_artist", label: "Audio loose in an artist folder (no album)", bad: true },
  { kind: "unexpected_folder", label: "Unexpected folder in the music folder root", bad: true },
  { kind: "unexpected_subfolder", label: "Unexpected folder inside an album", bad: true },
  { kind: "empty_album", label: "Album folder with no audio", bad: true },
  { kind: "empty_artist", label: "Artist folder with no albums", bad: true },
  { kind: "wrong_case", label: "Name capitalization differs from the naming script", bad: true },
  { kind: "legacy_state_file", label: "Leftover from the old .mlo_data layout", bad: false },
  { kind: "stray_in_artists", label: "Stray file directly in Artists/", bad: false },
  { kind: "hidden_folder", label: "Hidden folder inside Artists/", bad: false },
  { kind: "stray_file", label: "Stray file inside an album", bad: false },
];

/** What Apply fixes does with a row, in the panel's words. Static and
 *  string-keyed, so a Record: the values are sentences, not data. */
const FIX_LABEL_BY_ACTION: Record<string, string> = {
  rename: "will be renamed",
  move: "will be moved",
  trash: "will go to the Trash",
};

/** Library-layout audit: every place the music folder does not match
 *  `Artists/<Artist>/<Album>/<files>`.
 *
 *  Read-only until you say otherwise. A wrong guess here moves somebody's
 *  music, so the rows say what is where and how to fix it, and every fix the
 *  app offers is one the scan has already PROVED: an artist/album/file name
 *  spelled in the wrong letter case, audio sitting outside any album folder
 *  (its own tags name the album), and an artist folder with no albums — which
 *  cannot hold music at all and is removable to the app's Trash (never
 *  deleted, always restorable). Scan reports; Apply fixes does those three
 *  and reports what it left alone. */
function LayoutPanel() {
  const [report, setReport] = useState<LayoutReport | null>(null);
  // When the report on screen was scanned, and whether it is about a music
  // folder other than the configured one. The rows alone cannot say either:
  // script 20 stores its report for the Library page, and showing that here
  // is what makes "Run All found problems" and this panel the same answer.
  const [scannedAt, setScannedAt] = useState<string | null>(null);
  const [foreign, setForeign] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    // No toast on failure: a panel that cannot read the stored report simply
    // starts empty, which is exactly what the Scan button is for.
    api.libraryLayoutReport()
      .then((snap) => {
        if (!live || !snap.exists || !snap.report) return;
        if (snap.stale) {
          setForeign(snap.music_folder);
          return;
        }
        setReport(snap.report);
        setScannedAt(snap.scanned_at);
        setOpen(new Set(LAYOUT_KINDS.filter((k) => snap.report!.counts[k.kind]).map((k) => k.kind)));
      })
      .catch(() => { /* nothing stored yet */ });
    return () => {
      live = false;
    };
  }, []);

  const scan = async () => {
    setBusy(true);
    try {
      const r = await api.libraryLayout();
      setReport(r);
      setForeign(null);
      // Open the kinds that actually have rows, so a scan lands on its findings.
      setOpen(new Set(LAYOUT_KINDS.filter((k) => r.counts[k.kind]).map((k) => k.kind)));
      // The route stores the report it just produced; read that copy back so
      // the stamp shown is the SERVER's clock, not the browser's guess at it.
      const snap = await api.libraryLayoutReport();
      setScannedAt(snap.scanned_at);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  // Scan AND fix — the same walk as Scan plus the fixes it can prove, which is
  // why it sits next to it: what it will do is exactly what the rows above
  // say, and the summary it brings back says what it did. The report it
  // returns is the POST-fix one, so the rows below are what is still wrong.
  const apply = async () => {
    setBusy(true);
    try {
      const r = await api.libraryLayoutApply();
      setReport(r);
      setForeign(null);
      setOpen(new Set(LAYOUT_KINDS.filter((k) => r.counts[k.kind]).map((k) => k.kind)));
      const snap = await api.libraryLayoutReport();
      setScannedAt(snap.scanned_at);
      const fixed = r.fixed ?? 0;
      const failed = r.fix_failed ?? 0;
      // A failed fix is not a failed run: the library is still there, and the
      // row below says which file could not move and why. Reported apart from
      // the successes so neither hides the other.
      if (failed) toast.error(`Layout: ${fixed} fixed · ${failed} could not be fixed`);
      else toast.success(fixed ? `Layout fixed — ${fixed} change${fixed === 1 ? "" : "s"}` : "Library layout already correct");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const copyPath = async (p: string) => {
    try {
      await navigator.clipboard.writeText(p);
      setCopied(p);
      setTimeout(() => setCopied((c) => (c === p ? null : c)), 1500);
    } catch {
      toast("Could not reach the clipboard");
    }
  };

  const toggle = (kind: string) =>
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });

  const byKind = (kind: string): LayoutIssue[] =>
    (report?.issues ?? []).filter((i) => i.kind === kind);

  // The ONE row this panel may act on. An artist folder with no albums holds
  // nothing but the artist's own image and description, so removing it cannot
  // lose music — and it goes to the app's Trash, never to a delete, which is
  // what keeps it recoverable from the Trash page. Every other row stays
  // read-only: a wrong guess there moves somebody's music.
  const removeEmptyArtist = async (path: string) => {
    setBusy(true);
    try {
      const r = await api.libraryLayoutRemoveEmptyArtist(path);
      toast.success(`Moved to the Trash: ${r.trash}`);
      // Re-scan instead of editing the report in place: the panel's numbers
      // are the server's answer, and a row removed locally would leave the
      // counts and the Library page's warning describing a folder that is no
      // longer there.
      await scan();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="text-xs font-bold text-zinc-300 flex items-center gap-1.5 flex-1 min-w-0">
          <FolderTree className="h-3.5 w-3.5" /> Library layout
        </div>
        {report && (
          <span className="text-[11px] text-zinc-500">
            {report.audio_files} audio file(s) · {report.albums} album folder(s) · {report.artists} artist(s)
          </span>
        )}
        {/* When the report on screen was measured. Without it the rows look
            equally fresh whether Run All wrote them a minute or a month ago. */}
        {scannedAt && (
          <span className="text-[11px] text-zinc-500" title="When this report was scanned">
            scanned {new Date(scannedAt).toLocaleString()}
          </span>
        )}
        <button className="btn-primary !py-1 text-xs tap" disabled={busy} onClick={scan}>
          <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
          {busy ? "Scanning…" : report ? "Rescan" : "Scan library layout"}
        </button>
        {/* The fixing half, offered next to the scan: it rewalks and fixes,
            and it is only offered once a scan has shown there is a library to
            look at. What it will touch is what the rows below mark. */}
        {report?.exists && (
          <button className="btn-ghost !py-1 text-xs tap" disabled={busy} onClick={apply}
            title="Rename wrongly-cased names, move loose audio into its album folder, and send what is excess to the Trash — stray files, foreign folders holding no audio, empty album folders, album-less artist folders. Nothing is deleted, and the Trash page can put any of it back">
            <Wand2 className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
            {busy ? "Fixing…" : "Apply fixes"}
          </button>
        )}
      </div>

      <div className="text-[10px] text-zinc-600 mt-1.5">
        Walks the whole music folder and reports anything that is not
        <span className="font-mono text-zinc-500"> Artists/&lt;Artist&gt;/&lt;Album&gt;/ </span>
        — misplaced files, stray files, unexpected folders, empty albums, artist
        folders with no albums. Rows marked
        <span className="font-mono text-zinc-500"> will be renamed </span>/
        <span className="font-mono text-zinc-500"> moved </span>/
        <span className="font-mono text-zinc-500"> to the Trash </span>
        are the ones <span className="font-mono text-zinc-500">Apply fixes</span> settles by
        itself (script 20, Optimize library layout, does this on every import and every run
        too, unless the layout_apply setting is off); every other row is yours to fix.
        Nothing is ever deleted — a removal goes to the Trash, which lists it and can put
        it back.
      </div>

      {/* What the apply phase just did, in words. Only present on a report that
          came from an apply (script 20 or the button above): a plain scan has
          `fixes` absent, so this stays off and the rows below are the whole
          story. Skipped rows are not repeated here — they are the issue rows
          below, each with its own reason. */}
      {report?.fixes && (
        <div className="mt-3 border border-border rounded-lg overflow-hidden">
          <div
            className={`px-2.5 py-1.5 text-xs ${
              report.fix_failed ? "text-amber-200 bg-amber-950/30" : "text-emerald-300 bg-emerald-950/30"
            }`}
          >
            Apply: {report.fixed} fixed · {report.fix_failed} could not be fixed ·{" "}
            {report.skipped} left as they are
          </div>
          {(report.fixes.filter((f) => f.result !== "skipped")).length > 0 && (
            <div className="stagger divide-y divide-border/60">
              {report.fixes
                .filter((f) => f.result !== "skipped")
                .map((f) => (
                  <div key={`${f.kind}:${f.path}`} className="px-2.5 py-1 text-[11px] flex items-start gap-2">
                    <span
                      className={`shrink-0 font-mono ${
                        f.result === "fixed" ? "text-emerald-300" : "text-amber-200"
                      }`}
                    >
                      {f.result === "fixed" ? "fixed" : "failed"}
                    </span>
                    <span className="min-w-0 break-words text-zinc-400">{f.action}</span>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {/* A stored report of ANOTHER music folder is not this library's state:
          the rows would be about paths the app can no longer reach, so they
          are not shown at all and the scan is offered instead. */}
      {foreign && (
        <div className="mt-3 text-xs text-zinc-400 bg-raise border border-border rounded-lg px-3 py-2">
          The stored layout report describes <span className="font-mono">{foreign}</span>, not the music folder
          configured now — it is not shown. Scan to report on the current library.
        </div>
      )}

      {report && !report.exists && (
        <div className="mt-3 text-xs text-amber-200 bg-amber-950/30 border border-amber-900/60 rounded-lg px-3 py-2">
          The music folder is not set or does not exist.
        </div>
      )}

      {report && report.exists && report.total === 0 && (
        <div className="mt-3 text-xs text-emerald-300 bg-emerald-950/30 border border-emerald-900/60 rounded-lg px-3 py-2 flex items-center gap-2">
          <Check className="h-3.5 w-3.5" /> The library is laid out correctly — no misplaced files or unexpected folders.
        </div>
      )}

      {report && report.total > 0 && (
        <div className="mt-3 space-y-2">
          <div className="text-xs text-amber-200 bg-amber-950/30 border border-amber-900/60 rounded-lg px-3 py-2">
            {report.total} problem{report.total === 1 ? "" : "s"} found in{" "}
            {Object.keys(report.counts).length} categor{Object.keys(report.counts).length === 1 ? "y" : "ies"}.
          </div>
          {LAYOUT_KINDS.map(({ kind, label, bad }) => {
            const rows = byKind(kind);
            if (!rows.length) return null;
            const isOpen = open.has(kind);
            return (
              <div key={kind} className="border border-border rounded-lg overflow-hidden">
                <button
                  className="w-full flex items-center gap-2 px-2.5 py-1.5 text-xs bg-panel hover:bg-raise text-left tap"
                  onClick={() => toggle(kind)}
                >
                  {isOpen ? <Down className="h-3 w-3 shrink-0" /> : <Up className="h-3 w-3 shrink-0 rotate-90" />}
                  <span className={`flex-1 min-w-0 truncate ${bad ? "text-amber-200" : "text-zinc-300"}`}>{label}</span>
                  <span className="chip bg-raise border border-border text-zinc-400 shrink-0">{rows.length}</span>
                </button>
                {isOpen && (
                  <div className="stagger divide-y divide-border/60">
                    {rows.map((i) => (
                      <div key={i.abs} className="px-2.5 py-1.5 text-[11px] space-y-0.5">
                        <div className="flex items-center gap-2">
                          <span className="flex-1 min-w-0 break-words text-zinc-300 font-mono" title={i.abs}>
                            {i.path}
                          </span>
                          <button
                            className="btn-ghost !px-1.5 !py-0.5 text-[10px] tap shrink-0"
                            onClick={() => copyPath(i.abs)}
                            title={i.abs}
                          >
                            {copied === i.abs ? "copied" : "copy path"}
                          </button>
                          {/* What Apply fixes does with THIS row, said on the
                              row itself: a row carrying one is settled by the
                              script, and a row without one is the user's to
                              move (deleting is never offered). */}
                          {i.fix && (
                            <span
                              className="chip bg-raise border border-border text-zinc-400 shrink-0"
                              title={`Apply fixes: ${i.fix.action}${i.fix.to ? ` → ${i.fix.to}` : ""}`}
                            >
                              {FIX_LABEL_BY_ACTION[i.fix.action] ?? "will be fixed"}
                            </span>
                          )}
                          {/* The one action this panel offers per row, and only
                              on the kind that cannot hold music: an artist
                              folder with no albums. It moves the folder into
                              the app's Trash (nothing is deleted), so the Trash
                              page can put it back. */}
                          {kind === "empty_artist" && (
                            <button
                              className="btn-ghost !px-1.5 !py-0.5 text-[10px] tap shrink-0 text-red-300"
                              disabled={busy}
                              onClick={() => removeEmptyArtist(i.abs)}
                              title={
                                `Move ${i.abs} to the Trash — nothing is deleted, ` +
                                `the Trash page can restore it`
                              }
                            >
                              remove
                            </button>
                          )}
                        </div>
                        <div className="text-zinc-500">{i.detail} — {i.hint}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
