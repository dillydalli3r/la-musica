import type { ReactNode } from "react";
import { ChevronDown, ChevronUp, Eye, EyeOff, X } from "lucide-react";
import type { CfgField } from "../lib/configMeta";
import Switch from "./ui/switch";

/** The ONE renderer for a config field — the Settings page and the first-run
 *  wizard draw the same rows from the same groups (`lib/configMeta`), so
 *  "where do I change this later" always lands on a control that looks like
 *  the one the user met.
 *
 *  The shape is a ROW: the setting's name and its one-line explanation on the
 *  left, the control at a fixed right column (a switch, a select, a field).
 *  Anything whose control is wider than that column — an ordered provider
 *  list, a set of checkboxes, a comma list — stacks under its own label
 *  instead of being crushed into it. This replaced a three-column staggered
 *  grid of label-above-control boxes; the grid read as a wall of tiny
 *  controls and wrapped mid-group on every window size, because a field's
 *  neighbours changed with the viewport rather than with the group. */

/** One provider the pickers can offer; `rank` is its position in the
 *  BUILT-IN chain (a picked order replaces the order, never the rank). */
export interface ProviderOption {
  id: string;
  label: string;
  notes?: string;
  rank?: number;
}

/** One catalog behind a `list` field: the pickable providers plus the
 *  built-in order, which is what an empty list falls back to. */
export interface FieldCatalog {
  options: ProviderOption[];
  builtin: (k: string) => string[];
}

/** One section of a settings screen: a framed card with the group's title and
 *  its one-paragraph explanation, then the rows. The Settings page renders one
 *  per config group (plus its own bespoke sections — the folder picker, Run
 *  All, the dependency table), and both surfaces read the same heading shape,
 *  so a group met in the wizard is recognisable on the page that keeps it. */
export function CfgSection({ title, blurb, id, badge, children }: {
  title: ReactNode;
  blurb?: ReactNode;
  /** Anchor id, so a search hit can scroll to the section that owns a field. */
  id?: string;
  badge?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section id={id} className="panel">
      <div className="flex items-baseline gap-2 flex-wrap">
        <h2 className="text-sm font-semibold text-zinc-100">{title}</h2>
        {badge}
      </div>
      {blurb && <p className="text-[11px] text-zinc-500 leading-relaxed mt-1 max-w-3xl">{blurb}</p>}
      <div className="mt-2">{children}</div>
    </section>
  );
}

/** One row: label + explanation left, control right. `stacked` puts the
 *  control on its own line under the label — for controls that need the
 *  width (lists, matrices, anything with more than one input). */
export function CfgRow({ label, help, control, stacked = false }: {
  label: ReactNode;
  help?: ReactNode;
  control?: ReactNode;
  stacked?: boolean;
}) {
  const head = (
    <div className="min-w-0 flex-1">
      <div className="text-[13px] text-zinc-200">{label}</div>
      {help && <div className="text-[11px] text-zinc-500 leading-relaxed mt-0.5 max-w-3xl">{help}</div>}
    </div>
  );
  if (stacked) {
    return (
      <div className="py-2.5">
        {head}
        <div className="mt-2">{control}</div>
      </div>
    );
  }
  return (
    <div className="py-2.5 flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-6">
      {head}
      <div className="shrink-0 sm:w-56 sm:pt-0.5 flex sm:justify-end">{control}</div>
    </div>
  );
}

/** Ordered provider picker for a `list` config key: the listed providers are
 *  tried in order (first one with an answer wins). An empty list means the
 *  built-in order, shown as a placeholder. */
export function ProviderOrder({
  options, builtin, order, onChange,
}: {
  options: ProviderOption[];
  builtin: string[];
  order: string[];
  onChange: (next: string[]) => void;
}) {
  const move = (i: number, d: number) => {
    const next = order.slice();
    [next[i], next[i + d]] = [next[i + d], next[i]];
    onChange(next);
  };
  return (
    <div className="space-y-1.5">
      {order.length === 0 ? (
        <div className="rounded-md border border-dashed border-border px-2 py-1.5 text-[11px] text-zinc-500">
          <span className="text-zinc-400">Built-in order: </span>
          {builtin.map((id) => options.find((o) => o.id === id)?.label ?? id).join(" → ")}
        </div>
      ) : (
        <div className="rounded-md border border-border divide-y divide-border/60">
          {order.map((id, i) => {
            const opt = options.find((o) => o.id === id);
            return (
              <div key={id} className="flex items-start gap-2 px-2 py-1">
                <span className="w-4 pt-0.5 text-[10px] text-zinc-600">{i + 1}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[12px] text-zinc-200 truncate">{opt?.label ?? id}</span>
                    {opt?.rank !== undefined && (
                      <span
                        className="chip border border-accent/25 bg-accent/10 text-accent-soft shrink-0"
                        title="Rank in the BUILT-IN chain — this list replaces the order, it does not change the rank"
                      >
                        #{opt.rank} preferred
                      </span>
                    )}
                  </div>
                  {opt?.notes && <div className="text-[10px] text-zinc-600">{opt.notes}</div>}
                </div>
                <button
                  type="button"
                  className="min-h-8 min-w-8 md:min-h-0 md:min-w-0 flex items-center justify-center text-zinc-500 hover:text-white disabled:opacity-30"
                  title="Move up"
                  disabled={i === 0}
                  onClick={() => move(i, -1)}
                >
                  <ChevronUp className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  className="min-h-8 min-w-8 md:min-h-0 md:min-w-0 flex items-center justify-center text-zinc-500 hover:text-white disabled:opacity-30"
                  title="Move down"
                  disabled={i === order.length - 1}
                  onClick={() => move(i, 1)}
                >
                  <ChevronDown className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  className="min-h-8 min-w-8 md:min-h-0 md:min-w-0 flex items-center justify-center text-zinc-500 hover:text-red-300"
                  title="Remove from the list (unlisted providers are not used)"
                  onClick={() => onChange(order.filter((x) => x !== id))}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })}
        </div>
      )}
      {options.some((o) => !order.includes(o.id)) && (
        <div className="flex flex-wrap gap-1.5">
          {options.filter((o) => !order.includes(o.id)).map((o) => (
            <button
              key={o.id}
              type="button"
              className="chip border border-white/15 bg-white/5 text-[10px] text-zinc-400 hover:text-white tap"
              title={o.notes}
              onClick={() => onChange([...order, o.id])}
            >
              + {o.label}
            </button>
          ))}
        </div>
      )}
      {order.length > 0 && (
        <button
          type="button"
          className="text-[10px] text-zinc-500 hover:text-white underline tap"
          onClick={() => onChange([])}
        >
          Reset to the built-in order
        </button>
      )}
    </div>
  );
}

/** Reveal state for the password fields, one Set per surface. */
export type RevealState = {
  has: (k: string) => boolean;
  toggle: (k: string) => void;
};

export default function CfgFields({ fields, get, set, catalogs, optionsFor, reveal, highlight }: {
  fields: CfgField[];
  /** The value in force for a key (the draft on the wizard, the page's own
   *  config state on Settings). */
  get: (k: string) => unknown;
  set: (k: string, v: unknown) => void;
  /** Behind `type: "list"` — the catalog each `list` field names. */
  catalogs: Record<string, FieldCatalog>;
  /** Behind `optionsFrom` on select/multi fields (the cover countries, the
   *  app's locales, the genre chain's own sources): the option list the
   *  module cannot carry. */
  optionsFor?: (from: string) => [string, string][];
  reveal?: RevealState;
  /** The field a settings-search jump landed on: its row is flashed so the
   *  reader sees which of the four controls on screen answered. */
  highlight?: string | null;
}) {
  return (
    <div className="divide-y divide-border/50">
      {fields.map((f) => {
        const value = get(f.k);
        const flash = highlight === f.k;
        const frame = (label: ReactNode, control: ReactNode, stacked = false, help?: ReactNode) => (
          <div
            key={f.k}
            data-cfg-key={f.k}
            className={flash ? "rounded-md -mx-2 px-2 ring-1 ring-accent/60 bg-accent/5" : undefined}
          >
            <CfgRow label={label} help={help ?? f.help} control={control} stacked={stacked} />
          </div>
        );

        if (f.type === "list") {
          const raw = value;
          const order = Array.isArray(raw)
            ? raw.map(String)
            : typeof raw === "string"
              ? raw.split(/[,;\s]+/).filter(Boolean)
              : [];
          const cat = catalogs[f.catalog];
          return frame(
            f.label,
            <ProviderOrder
              options={cat?.options ?? []}
              builtin={cat?.builtin(f.k) ?? []}
              order={order}
              onChange={(next) => set(f.k, next)}
            />,
            true
          );
        }

        if (f.type === "multi") {
          const on = Array.isArray(value) ? (value as unknown[]).map(String) : [];
          const base = f.options.length ? f.options : (f.optionsFrom ? optionsFor?.(f.optionsFrom) ?? [] : []);
          // An id the catalogue does not list (a source the chain dropped, or
          // the health payload not arrived yet) still has to be visible, or a
          // saved value would silently disappear from the picker.
          const opts: [string, string][] = [
            ...base,
            ...on.filter((v) => !base.some(([o]) => o === v)).map((v): [string, string] => [v, v]),
          ];
          return frame(
            f.label,
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {opts.map(([v, l]) => (
                <label key={v} className="flex items-center gap-2 text-[13px] text-zinc-300 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={on.includes(v)}
                    onChange={(e) => set(f.k, e.target.checked ? [...on, v] : on.filter((x) => x !== v))}
                  />
                  {l}
                </label>
              ))}
            </div>,
            true
          );
        }

        if (f.type === "csv") {
          // Edited as text, stored as the string list the config expects;
          // blank entries are dropped by the backend's own normalization.
          const parts = Array.isArray(value) ? (value as unknown[]).map(String) : [];
          return frame(
            f.label,
            <input
              className="input !py-1 text-xs w-full tap"
              value={parts.join(", ")}
              onChange={(e) => set(f.k, e.target.value.split(",").map((s) => s.trim()))}
            />,
            true
          );
        }

        if (f.type === "matrix") {
          // A per-filetype grid of switches: config holds
          // `{ filetype: { row: bool } }` (audio_tag_writes, encoder_tags).
          const grid = (value ?? {}) as Record<string, Record<string, boolean>>;
          return frame(
            f.label,
            <div className="rounded-md border border-border table-scroll">
              <table className="w-full text-[11px]">
                <thead>
                  <tr>
                    <th className="th !py-1"> </th>
                    {f.cols.map(([c, cl]) => (
                      <th key={c} className="th !py-1 text-center">{cl}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {f.rows.map(([r, rl]) => (
                    <tr key={r} className="border-t border-border/60">
                      <td className="td !py-1 text-zinc-400 whitespace-nowrap">{rl}</td>
                      {f.cols.map(([c]) => (
                        <td key={c} className="td !py-1 text-center">
                          <input
                            type="checkbox"
                            checked={grid[c]?.[r] !== false}
                            onChange={(e) =>
                              set(f.k, {
                                ...grid,
                                [c]: { ...(grid[c] ?? {}), [r]: e.target.checked },
                              })
                            }
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>,
            true
          );
        }

        if (f.type === "chain") {
          // A list of script ids ticked in execution order — the control needs
          // the script table and the run-order state, which live on the page
          // that owns them, so the page passes it through `chainControl`.
          return null;
        }

        if (f.type === "bool") {
          return frame(
            f.label,
            <Switch checked={!!value} onCheckedChange={(v) => set(f.k, v)} />
          );
        }

        if (f.type === "select") {
          const base = f.options.length ? f.options : (f.optionsFrom ? optionsFor?.(f.optionsFrom) ?? [] : []);
          const current = String(value ?? "");
          // A value the option list does not carry (a config written by an
          // older or newer backend) still has to be visible, or the control
          // would look unset while the config holds a real value.
          const shown: [string, string][] =
            !current || base.some(([v]) => v === current) ? base : [[current, current], ...base];
          return frame(
            f.label,
            // min-h-8 (32px): a `!py-1 text-xs` select lands at 27px, under the
            // phone tap floor check_responsive sweeps for — the same floor the
            // app's full-size `.input` clears.
            <select
              className="input !py-1 text-xs w-full sm:w-56 min-h-8"
              value={current}
              onChange={(e) => set(f.k, e.target.value)}
            >
              {shown.map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          );
        }

        if (f.type === "password") {
          const shown = reveal?.has(f.k) ?? false;
          return frame(
            f.label,
            <div className="relative w-full sm:w-56">
              <input
                className="input !py-1 !pr-9 text-xs w-full"
                type={shown ? "text" : "password"}
                value={String(value ?? "")}
                onChange={(e) => set(f.k, e.target.value)}
                spellCheck={false}
              />
              {reveal && (
                <button
                  type="button"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 p-[9px] -mx-[9px] text-zinc-500 hover:text-zinc-200"
                  title={shown ? "Hide password" : "Show password"}
                  onClick={() => reveal.toggle(f.k)}
                >
                  {shown ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                </button>
              )}
            </div>
          );
        }

        if (f.type === "number") {
          return frame(
            f.label,
            <input
              className="input !py-1 text-xs w-full sm:w-28 text-right"
              type="number"
              min={f.min}
              max={f.max}
              step={f.step ?? 1}
              value={String(value ?? "")}
              onChange={(e) => set(f.k, e.target.value === "" ? "" : Number(e.target.value))}
            />
          );
        }

        // text
        return frame(
          f.label,
          <input
            className="input !py-1 text-xs w-full sm:w-56"
            value={Array.isArray(value) ? (value as unknown[]).join("; ") : String(value ?? "")}
            onChange={(e) => set(f.k, e.target.value)}
            spellCheck={false}
          />
        );
      })}
    </div>
  );
}