import { useI18n, type MessageKey } from "../lib/i18n";

/** The donation page's unpaid staff: a few cats doing their best to get in
 *  the way of a crypto address. Each one is two lines of face, one line of
 *  art and one joke — a wall of ASCII would bury the addresses the page
 *  exists to show, so the cats stay decoration and the addresses stay plain
 *  text. The art is deliberately short enough to sit in a single card row;
 *  the line under it is the part that gets translated. */

export type CatId = "paw" | "inspect" | "keyboard" | "box";

const CATS: { id: CatId; line: MessageKey; art: string }[] = [
  {
    id: "paw",
    line: "donations.cat.paw",
    art: " /\\_/\\\n( o.o )っ 🐾\n  > ^ <",
  },
  {
    id: "inspect",
    line: "donations.cat.inspect",
    art: " /\\_/\\\n( ⌐‿⌐ )\n  > ^ < ?",
  },
  {
    id: "keyboard",
    line: "donations.cat.keyboard",
    art: " /\\_/\\\n( -.- )~~~~\n  > ^ <",
  },
  {
    id: "box",
    line: "donations.cat.box",
    art: " /\\_/\\\n( $.$ )\n [____] 📦",
  },
];

/** Whimsical, not animated: an endless tail-wag is a CPU bill nobody asked
 *  for, and the app's quiet is what makes the joke land. `pre` keeps the
 *  art's own spacing, and the block is monospaced so the faces line up at
 *  any font size. */
export default function Cats({ ids }: { ids: CatId[] }) {
  const { t } = useI18n();
  return (
    <div className={`grid gap-3 ${ids.length > 1 ? "sm:grid-cols-3" : ""}`}>
      {ids.map((id) => {
        const cat = CATS.find((c) => c.id === id);
        if (!cat) return null;
        return (
          <div key={id} className="panel flex items-center gap-3">
            {/* Decorative: the joke is in the sentence, which is also the part
                a screen reader can actually deliver. */}
            <pre aria-hidden="true" className="shrink-0 font-mono text-[11px] leading-tight text-accent-soft">
              {cat.art}
            </pre>
            <p className="text-xs leading-relaxed text-zinc-400">{t(cat.line)}</p>
          </div>
        );
      })}
    </div>
  );
}
