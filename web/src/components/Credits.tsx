import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { useState } from "react";
import Modal from "./Modal";
import { useI18n } from "../lib/i18n";

/** One credit row as the data file spells it. */
export interface CreditItem {
  name: string;
  license: string;
  url: string;
  use?: string;
}

interface CreditGroup {
  title: string;
  items: CreditItem[];
}

/** The credit data is a static file the app also renders in Settings — one
 *  source, so a new provider is credited by adding it there. Read through
 *  react-query so both surfaces share the fetch. */
export function useCredits() {
  return useQuery({
    queryKey: ["credits"],
    staleTime: Infinity,
    queryFn: async (): Promise<CreditGroup[]> => {
      try {
        const r = await fetch("/credits.json");
        return ((await r.json()) as { groups?: CreditGroup[] }).groups ?? [];
      } catch {
        return [];
      }
    },
  });
}

/** How many service names the corner itself shows before the rest move
 *  behind the popover: two wrapped lines is the budget for a footer that
 *  never pushes the navigation around. */
const INLINE_SERVICES = 8;

/** Bottom-left credit strip.
 *
 *  Everything this app does with music, it does with someone else's service,
 *  database or tool — their licences require the credit and it belongs in the
 *  UI, not only in a file. The corner names the services it talks to (each a
 *  link out to the project), and the popover carries the full list: every
 *  service, vendored tool, package and font, with the licence behind each.
 *
 *  `collapsed` (the icon-only rail) swaps the names for the same trigger as a
 *  small icon button, so the credits never disappear with the sidebar. */
export default function CreditsFooter({ collapsed = false }: { collapsed?: boolean }) {
  const { t } = useI18n();
  const { data: groups = [] } = useCredits();
  const [open, setOpen] = useState(false);
  const services = groups.find((g) => g.title.startsWith("Services"))?.items ?? [];
  const inline = services.slice(0, INLINE_SERVICES);
  const rest = services.length - inline.length;

  return (
    <div className={collapsed ? "px-2 pb-2" : "px-3 pb-2"}>
      {!collapsed && (
        <div className="text-[10px] leading-relaxed text-zinc-600">
          <div className="text-zinc-500">{t("credits.from")}</div>
          <div className="mt-0.5 flex flex-wrap gap-x-1 gap-y-0.5">
            {inline.map((c, i) => (
              <span key={c.name} className="whitespace-nowrap">
                {/* Every link leaves the app: these are other people's sites,
                    and opening one inside the shell would navigate away from
                    the library. */}
                <a
                  href={c.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-zinc-500 hover:text-accent-soft hover:underline"
                  title={`${c.name} — ${c.use || c.license}`}
                >
                  {c.name}
                </a>
                {i < inline.length - 1 && <span className="text-zinc-700"> ·</span>}
              </span>
            ))}
          </div>
        </div>
      )}
      <button
        className={`mt-1 rounded-lg border border-transparent text-zinc-600 hover:text-white hover:bg-raise hover:border-border transition-colors ${
          collapsed ? "p-1.5 w-full flex justify-center" : "flex items-center gap-1.5 px-1.5 py-1 text-[10px]"
        }`}
        onClick={() => setOpen(true)}
        title={t("credits.open")}
        aria-haspopup="dialog"
      >
        <Info className="h-3 w-3 shrink-0" />
        {!collapsed && <span>{rest > 0 ? t("credits.more", { n: rest }) : t("credits.title")}</span>}
      </button>
      {open && <CreditsDialog groups={groups} onClose={() => setOpen(false)} />}
    </div>
  );
}

/** The full credit list, on the shared dialog shell (backdrop click, Escape,
 *  focus trap — the same as every other modal in the app). */
function CreditsDialog({ groups, onClose }: { groups: CreditGroup[]; onClose: () => void }) {
  const { t } = useI18n();
  return (
    <Modal
      onClose={onClose}
      title={t("credits.title")}
      icon={Info}
      subtitle={t("credits.subtitle")}
      width="max-w-3xl"
      bodyClass="px-4 py-4 space-y-4"
    >
      {groups.map((g) => (
        <section key={g.title}>
          <h3 className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">{g.title}</h3>
          <ul className="mt-1 grid gap-x-4 gap-y-0.5" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))" }}>
            {g.items.map((c) => (
              <li key={c.name} className="min-w-0">
                <a
                  href={c.url}
                  target="_blank"
                  rel="noreferrer"
                  className="block truncate text-[11px] text-zinc-400 hover:text-accent-soft"
                  title={`${c.name} — ${c.use || c.license} · ${c.url}`}
                >
                  <span className="text-zinc-200">{c.name}</span>
                  <span className="text-zinc-600"> · {c.license}</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ))}
      <p className="text-[10px] leading-relaxed text-zinc-600">
        {t("credits.legal")}
      </p>
    </Modal>
  );
}
