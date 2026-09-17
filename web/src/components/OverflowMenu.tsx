import { useState } from "react";
import { Ellipsis } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import Popover, { MenuItem } from "./Popover";

export interface OverflowMenuItem {
  label: string;
  icon?: LucideIcon;
  onClick?: () => void;
  danger?: boolean;
  disabled?: boolean;
  hidden?: boolean;
  title?: string;
}

export interface OverflowMenuSection {
  title?: string;
  items: OverflowMenuItem[];
}

/** "…" overflow menu with grouped sections — keeps page headers to the
 *  primary action plus one button. A thin wrapper over the shared Popover
 *  (shield + Escape + entry animation all live there). */
export default function OverflowMenu({
  sections,
  buttonTitle = "More actions",
  buttonClass = "btn-ghost !px-2.5",
  align = "right",
}: {
  sections: OverflowMenuSection[];
  buttonTitle?: string;
  buttonClass?: string;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);

  const visible = sections
    .map((s) => ({ ...s, items: s.items.filter((i) => !i.hidden) }))
    .filter((s) => s.items.length);

  return (
    <div className="relative">
      <button
        className={buttonClass}
        onClick={() => setOpen(!open)}
        title={buttonTitle}
        aria-label={buttonTitle}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Ellipsis className="h-4 w-4" />
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        align={align}
        panelClass="w-64 max-h-[70vh] overflow-y-auto p-1.5"
      >
        {visible.map((s, si) => (
          <div key={si} className={si > 0 ? "mt-1 pt-1 border-t border-white/10" : ""}>
            {s.title && (
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2.5 pt-1 pb-0.5">{s.title}</div>
            )}
            {s.items.map((it, ii) => (
              <MenuItem
                key={ii}
                label={it.label}
                icon={it.icon}
                danger={it.danger}
                disabled={it.disabled}
                title={it.title}
                onClick={() => {
                  setOpen(false);
                  it.onClick?.();
                }}
              />
            ))}
          </div>
        ))}
      </Popover>
    </div>
  );
}
