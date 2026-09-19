import type { ComponentType } from "react";

export interface SegmentedOption<T extends string> {
  id: T;
  label: string;
  icon?: ComponentType<{ className?: string }>;
}

/** The app's one segmented-control style — the same look the Library view
 * switcher and the Favorites tabs already use, shared so every tab row /
 * mode switcher reads identically. */
export default function Segmented<T extends string>({ value, onChange, options, className }: {
  value: T;
  onChange: (id: T) => void;
  options: readonly SegmentedOption<T>[];
  className?: string;
}) {
  return (
    <div className={`flex w-fit rounded-md border border-border overflow-hidden ${className ?? ""}`} role="tablist">
      {options.map((o) => (
        <button
          key={o.id}
          role="tab"
          aria-selected={value === o.id}
          onClick={() => onChange(o.id)}
          className={`px-3 py-1.5 text-xs font-medium inline-flex items-center gap-1.5 transition-colors tap whitespace-nowrap ${
            value === o.id ? "bg-accent on-accent" : "bg-panel text-zinc-400 hover:text-white"
          }`}
        >
          {o.icon && <o.icon className="h-3.5 w-3.5" />}
          {o.label}
        </button>
      ))}
    </div>
  );
}
