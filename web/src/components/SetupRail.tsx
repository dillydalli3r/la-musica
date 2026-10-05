import { Check } from "lucide-react";

/** The setup rail — the house idiom for "where am I in this wizard", shared by
 *  every wizard a shell shows so the sequence never changes shape mid-flow.
 *
 *  `pages/SetupPage.tsx` and `pages/ClientSetup.tsx` each drew this inline, and
 *  the first-run backend question drew a third, shorter variant of its own:
 *  clicking "use the built-in backend" then cut a user from a two-chip rail to
 *  a five-chip one, which reads as a different screen rather than the next
 *  step of the same one. Steps behind are ticked, the current one is the
 *  accent chip.
 */
export default function SetupRail<T extends string>({
  steps,
  current,
  labels,
  onSelect,
  isLocked,
}: {
  steps: readonly T[];
  current: T;
  labels: Record<T, string>;
  /** When given, every chip is a button that jumps to its step — the form
   *  `pages/SetupPage.tsx` needs; without it the rail is the read-only
   *  progress idiom the shell wizards use. */
  onSelect?: (id: T) => void;
  /** Steps the wizard will not let a user past yet (a first run with no
   *  password set). Only consulted when `onSelect` is given. */
  isLocked?: (id: T) => boolean;
}) {
  const index = steps.indexOf(current);
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
      {steps.map((id, i) => {
        const chip = (
          <>
            <span
              className={`h-5 w-5 rounded-sm flex items-center justify-center text-[10px] border ${
                id === current
                  ? "bg-accent text-[var(--accent-fg)] border-accent"
                  : i < index
                    ? "bg-emerald-900/60 text-emerald-300 border-emerald-800"
                    : "bg-panel border-border text-zinc-500"
              }`}
            >
              {i < index ? <Check className="h-3 w-3" /> : i + 1}
            </span>
            <span className={id === current ? "text-zinc-200" : "text-zinc-600"}>{labels[id]}</span>
          </>
        );
        if (!onSelect) {
          return (
            <div key={id} className="flex items-center gap-2">
              {chip}
            </div>
          );
        }
        const locked = isLocked?.(id) ?? false;
        return (
          <button
            key={id}
            type="button"
            className="flex items-center gap-2 tap disabled:opacity-50"
            disabled={locked}
            onClick={() => onSelect(id)}
          >
            {chip}
          </button>
        );
      })}
    </div>
  );
}