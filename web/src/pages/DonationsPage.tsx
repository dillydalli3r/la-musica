import { useRef } from "react";
import { Coffee, Copy } from "lucide-react";
import PageHeader from "../components/PageHeader";
import { toast } from "../store";
import { useI18n, type MessageKey } from "../lib/i18n";

/** Sidebar "Donations" — the two addresses the maintainer actually accepts,
 *  and the honest version of why they are there. Nothing on this page is a
 *  paywall: the app is free and self-hosted, and the page says so outright
 *  rather than hinting at features that are not there anyway.
 *
 *  The addresses are printed verbatim — never trimmed, re-cased or checked
 *  against a service. A "helpful" rewrite of a crypto address is how money
 *  goes missing, so the only transformation that ever happens is the copy
 *  button handing the string to the clipboard unchanged. */
const COINS: { id: "litecoin" | "bitcoin"; name: MessageKey; note: MessageKey; address: string }[] = [
  {
    id: "litecoin",
    name: "donations.litecoin",
    note: "donations.litecoin_note",
    address: "LRisZa9HYBKE2sUc3VELYZq2WnyYtG6Jvu",
  },
  {
    id: "bitcoin",
    name: "donations.bitcoin",
    note: "donations.bitcoin_note",
    address: "bc1qf2snsus59ydvmk8rwp09e698gxjdlmyxyrnycu",
  },
];

/** Select an element's text the way a user would with the mouse. This is the
 *  fallback path, not an exotic one: the Clipboard API only exists in a
 *  secure context, and this app is normally served over plain http on the
 *  LAN, where `navigator.clipboard` is simply undefined. */
function selectContents(el: HTMLElement | null) {
  if (!el) return;
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
}

export default function DonationsPage() {
  const { t } = useI18n();
  // One entry per address, plus the stack that holds both, so the manual
  // fallback can select exactly what the button claimed to copy.
  const codeRefs = useRef<Record<string, HTMLElement | null>>({});
  const stackRef = useRef<HTMLDivElement | null>(null);

  const copy = async (text: string, label: string, fallback: HTMLElement | null) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(t("donations.copied", { coin: label }));
    } catch {
      selectContents(fallback);
      toast.info(t("donations.copy_manual"));
    }
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-3xl">
      <PageHeader
        icon={Coffee}
        title={t("donations.title")}
        subtitle={t("donations.subtitle")}
        actions={
          <button
            className="btn-ghost !py-1 text-xs tap"
            onClick={() =>
              copy(
                COINS.map((c) => c.address).join("\n"),
                t("donations.both"),
                stackRef.current
              )
            }
          >
            <Copy className="h-3 w-3" /> {t("donations.copy_both")}
          </button>
        }
      />

      <section className="panel-hero space-y-2">
        <h2 className="text-sm font-semibold text-zinc-200">{t("donations.why_title")}</h2>
        <p className="text-xs leading-relaxed text-zinc-400">{t("donations.why_body")}</p>
        <p className="text-xs leading-relaxed text-accent-soft">{t("donations.why_gated")}</p>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-zinc-200">{t("donations.addresses_title")}</h2>
        <p className="text-xs leading-relaxed text-zinc-500">{t("donations.addresses_help")}</p>

        <div ref={stackRef} className="space-y-3">
          {COINS.map((coin) => (
            <div key={coin.id} className="panel space-y-2">
              <div className="flex flex-wrap items-baseline justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-zinc-200">{t(coin.name)}</div>
                  <div className="text-[11px] text-zinc-500">{t(coin.note)}</div>
                </div>
                <button
                  className="btn-primary !py-1 text-xs tap"
                  onClick={() => copy(coin.address, t(coin.name), codeRefs.current[coin.id] ?? null)}
                >
                  <Copy className="h-3 w-3" /> {t("donations.copy")}
                </button>
              </div>
              {/* `select-all` so one click picks the whole address: copying it
                  half by hand is the mistake this block exists to prevent. */}
              <code
                ref={(el) => {
                  codeRefs.current[coin.id] = el;
                }}
                className="block select-all break-all rounded-lg border border-border bg-raise px-3.5 py-2.5 font-mono text-xs text-zinc-200"
              >
                {coin.address}
              </code>
            </div>
          ))}
        </div>

        <p className="text-[11px] text-zinc-600">{t("donations.thanks")}</p>
      </section>

      {/* The maintainer's cat, and the only ask on the page that needs no
          address. It replaced the row of ASCII cats: the joke was worth the
          space it took, but a photograph of the actual animal says the same
          thing in one glance, and the page is a donations page — the reader
          came for the addresses above (issue #44). */}
      <section>
        <div className="panel flex flex-col sm:flex-row items-center gap-4">
          <img
            src="/cat.jpg"
            alt={t("donations.cat_alt")}
            className="w-44 sm:w-48 h-auto rounded-lg shrink-0"
            loading="lazy"
          />
          <p className="text-sm leading-relaxed text-zinc-300">{t("donations.cat_line")}</p>
        </div>
      </section>
    </div>
  );
}
