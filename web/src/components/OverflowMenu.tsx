import { useRef, useState } from "react";
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
  buttonClass = "btn-ghost !px-2.5 tap",
  align = "right",
  icon: Icon = Ellipsis,
  label,
  fixed = true,
}: {
  sections: OverflowMenuSection[];
  buttonTitle?: string;
  buttonClass?: string;
  align?: "left" | "right";
  /** Trigger glyph. Defaults to the "…" a generic menu wears; a menu with a
   *  specific job (the tag actions) passes its own so two menus side by side
   *  are not two identical ellipses. */
  icon?: LucideIcon;
  /** Optional text next to the glyph — what makes a specialist menu read as a
   *  labelled action instead of a generic overflow. */
  label?: string;
  /** Portal the panel to the viewport and position it from this button — for
   *  a trigger inside an `overflow` container that would clip the flyout, or
   *  under the sidebar's z-index (the album cover menu: the panel used to be
   *  cut off on its left edge). Fixed panels also get the primitive's gutter
   *  flip on a narrow window and its cap to the room the trigger actually
   *  leaves, which is what makes a long menu (the track menu) scroll INSIDE
   *  the window instead of running past the fold. On by default: every caller
   *  of this component is a menu that can outgrow the row it hangs from, and
   *  an in-place panel is the exception a caller opts into with
   *  `fixed={false}`. */
  fixed?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);

  const visible = sections
    .map((s) => ({ ...s, items: s.items.filter((i) => !i.hidden) }))
    .filter((s) => s.items.length);

  return (
    <div className="relative">
      <button
        ref={btnRef}
        className={buttonClass}
        onClick={() => setOpen(!open)}
        title={buttonTitle}
        aria-label={buttonTitle}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Icon className="h-4 w-4" />
        {label ? <span className="text-xs">{label}</span> : null}
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        align={align}
        fixed={fixed}
        anchorRef={btnRef}
        panelClass="w-64 p-1.5"
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
