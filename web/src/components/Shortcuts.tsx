import { Keyboard } from "lucide-react";
import Modal from "./Modal";

/** One row of the shortcut sheet: the keys as they are pressed, and what the
 *  app does. Keys are written the way a keyboard shows them (Space, F, ?) so
 *  the sheet and the handlers cannot drift into different dialects. */
export interface Shortcut {
  keys: string[];
  label: string;
  where?: string;
}

/** Every global binding in the app, in one list — the sheet below renders it
 *  and the README documents it, so a new key is added once. Per-surface keys
 *  (the lyrics stamping editor's configurable map, the modal Escape) are named
 *  with their `where` instead of being spelled out key by key. */
export const SHORTCUTS: Shortcut[] = [
  { keys: ["/"], label: "Jump to search", where: "anywhere" },
  { keys: ["?"], label: "This shortcut sheet", where: "anywhere" },
  { keys: ["Esc"], label: "Close a dialog or menu", where: "anywhere" },
];

/** The shortcut sheet, on the shared dialog shell (backdrop click, Escape,
 *  focus trap — the same as every other modal in the app). */
export default function ShortcutsOverlay({ onClose }: { onClose: () => void }) {
  return (
    <Modal
      onClose={onClose}
      title="Keyboard shortcuts"
      icon={Keyboard}
      width="max-w-lg"
      bodyClass="px-0 py-1"
      footer={
        <p className="text-[10px] text-zinc-600">
          The lyrics editor has its own stamping keys.
          Shortcuts never fire while you are typing in a field.
        </p>
      }
    >
      <ul className="divide-y divide-border/60">
        {SHORTCUTS.map((s) => (
          <li key={s.keys.join("+")} className="flex items-center gap-3 px-4 py-2 text-xs">
            <span className="flex shrink-0 items-center gap-1">
              {s.keys.map((k) => (
                <kbd
                  key={k}
                  className="rounded border border-border bg-raise px-1.5 py-0.5 font-mono text-[10px] text-zinc-200"
                >
                  {k}
                </kbd>
              ))}
            </span>
            <span className="text-zinc-300">{s.label}</span>
            {s.where && <span className="ml-auto text-[10px] text-zinc-600">{s.where}</span>}
          </li>
        ))}
      </ul>
    </Modal>
  );
}
