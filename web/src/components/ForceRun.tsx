import { useRef, useState } from "react";
import { Check, ChevronDown, Zap } from "lucide-react";
import Popover from "./Popover";
import { FORCE_SCRIPTS, loadForceSel, saveForceSel } from "../lib/force";

/** One-shot force state (toggle + per-script selection) shared by Run All.
 * Extracted from the old header so any surface reuses the same behavior. */
export function useForceRun() {
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

/** The Force switch: a toggle plus a chevron menu that picks which scripts
 *  the one-shot Force applies to. The caller keeps the state (and decides
 *  whether a run sends `forceDict(forceSel)`). */
export function ForceControl({
  forceRun,
  toggle,
  forceSel,
  setSel,
}: {
  forceRun: boolean;
  toggle: () => void;
  forceSel: Record<string, boolean>;
  setSel: (next: Record<string, boolean>) => void;
}) {
  const [forceMenu, setForceMenu] = useState(false);
  // The Force menu is the tallest flyout in the app (14 flags + its All/None
  // row): measured from this trigger it opens into whatever room the window
  // leaves and scrolls inside it, instead of running past the bottom edge with
  // its lower flags unreachable (#53).
  const forceBtn = useRef<HTMLButtonElement>(null);
  return (
    <div className="relative flex items-center">
      <button
        className={`btn-ghost text-xs tap rounded-r-none border-r-0 ${forceRun ? "!text-accent border border-accent/50" : ""}`}
        onClick={toggle}
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
        ref={forceBtn}
        className={`btn-ghost text-xs tap min-w-11 md:min-w-0 rounded-l-none !px-1 ${forceRun ? "!text-accent" : ""}`}
        onClick={() => setForceMenu(!forceMenu)}
        title="Choose which scripts are forced"
        aria-haspopup="menu"
        aria-expanded={forceMenu}
      >
        <ChevronDown className="h-3.5 w-3.5" />
      </button>
      <Popover
        open={forceMenu}
        onClose={() => setForceMenu(false)}
        align="left"
        fixed
        anchorRef={forceBtn}
        panelClass="w-60 p-1.5"
      >
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
  );
}
