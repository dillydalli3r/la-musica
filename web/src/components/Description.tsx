import { type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";

/** Collapsed preview height, in lines. The stored text can be a whole
 *  Wikipedia article or biography, so the page shows a few lines with a fade
 *  and a Read more toggle instead of a wall of text. */
const PREVIEW_LINES = 5;

/** A markdown inline link `[label](url)`, or a bare `http(s)://…` url. The
 *  target may not hold whitespace, but a BALANCED parenthesis run is part of
 *  it — every disambiguated Wikipedia page is `/wiki/Foo_(bar)`, and the
 *  fetcher writes exactly that link. Half-written brackets stay plain text. */
const LINK_RE = /\[([^\]\n]+)\]\(([^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)|https?:\/\/[^\s<>"']+/g;

/** Trailing sentence punctuation (and an unbalanced `)`, as in a url glued to
 *  a parenthetical) is not part of the url. */
function trimUrl(url: string) {
  let out = url.replace(/[.,;:!?]+$/, "");
  while (out.endsWith(")") && out.split(")").length > out.split("(").length) out = out.slice(0, -1);
  return out;
}

/** A description from a source is user-editable and arrives over the network,
 *  so it is never injected as HTML: only `http(s)`/`mailto` become links, and
 *  anything malformed or unsafe stays the literal text it was. */
function inline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(LINK_RE)) {
    const at = m.index;
    const [raw, label, target] = m;
    const url = trimUrl(target ?? raw);
    if (!/^(?:https?:|mailto:)/i.test(url)) continue; // stays literal text below
    // a url straight after `](` belongs to an unterminated `[label](` — leave
    // the whole half-written link as text rather than half-parsing it
    if (!label && text.slice(at - 2, at) === "](") continue;
    if (at > last) nodes.push(text.slice(last, at));
    nodes.push(
      <a
        key={at}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        title={url}
        className="text-accent-soft underline decoration-dotted underline-offset-2 hover:decoration-solid break-words"
      >
        {label ?? url}
      </a>,
    );
    // for a bare url the punctuation trimmed above stays in the text
    last = at + (label ? raw.length : url.length);
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

/** `== Section ==` … `====== Deep ======`: a bare line wrapped in 2–6 `=` on
 *  each side is a section heading and the marker count is its level. The
 *  backreference makes a mismatched run (`== a ===`) plain text, like any
 *  other malformed markup. */
const HEADING_RE = /^\s*(={2,6})\s*(.+?)\s*\1\s*$/;

/** The heading tag per depth: descriptions sit under the page's own <h1>/<h2>
 *  (artist or album title), so a level-2 marker is the top of the block. */
const HEADING_TAGS = ["h3", "h4", "h5", "h6"] as const;

/** Modest and brighter than the body text: a section of a description, not an
 *  article title. Deeper levels step back down. */
const HEADING_CLASS = [
  "text-zinc-100 font-semibold",
  "text-zinc-200 font-medium",
  "text-zinc-200 font-medium",
  "text-zinc-300 font-medium",
];

type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; text: string; level: number };

/** Split a stored description into blocks, one per non-empty line — the same
 *  split this component always used — with a `== … ==` line becoming a
 *  heading instead of a paragraph. */
function blocks(text: string): Block[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = HEADING_RE.exec(line);
      // a marker pair with no title at all (`==  ==`) is not a heading
      return m && m[2].trim()
        ? { kind: "h", text: m[2], level: m[1].length }
        : { kind: "p", text: line };
    });
}

/** A stored description (artist biography / album blurb), collapsed to a
 *  clamped preview and expanded on demand.
 *
 *  Remount with React `key` when the entity being described changes, so a new
 *  artist/album starts collapsed; a react-query refetch of the SAME entity
 *  leaves the component mounted and keeps whatever the reader opened. */
export default function Description({ text }: { text: string }) {
  const bodyId = useId();
  const bodyRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);

  // Overflow test on the real element: under line-clamp, Chromium still reports
  // the full text in scrollHeight while clientHeight is the clamped height.
  // Skipped while expanded (nothing overflows then) so `clipped` keeps its last
  // value and the Show less control stays put.
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || expanded) return;
    const check = () => setClipped(el.scrollHeight > el.clientHeight + 1);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text, expanded]);

  // The sources hand over plain text with the original line and paragraph
  // breaks, not HTML: one <p> per paragraph, one heading per `== … ==` line.
  const body = blocks(text);

  return (
    <div className="mt-2 text-sm text-zinc-300 leading-relaxed">
      <div className="relative">
        <div
          id={bodyId}
          ref={bodyRef}
          title={`${text.length} characters`}
          className="space-y-2"
          style={
            expanded
              ? undefined
              : {
                  display: "-webkit-box",
                  WebkitBoxOrient: "vertical",
                  WebkitLineClamp: PREVIEW_LINES,
                  overflow: "hidden",
                }
          }
        >
          {body.map((b, i) => {
            if (b.kind === "p") return <p key={i}>{inline(b.text)}</p>;
            // the space-y-2 above is the paragraph gap; `!mt-1` keeps the
            // heading tight over its section (top margin < the bottom one)
            const depth = Math.min(b.level - 2, HEADING_TAGS.length - 1);
            const Tag = HEADING_TAGS[depth];
            return (
              <Tag key={i} className={`!mt-1 ${HEADING_CLASS[depth]}`}>
                {inline(b.text)}
              </Tag>
            );
          })}
        </div>
        {/* soft fade over the cut-off line, only while the text is clamped */}
        {clipped && !expanded && (
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-[#0a0a0c] to-transparent"
          />
        )}
      </div>
      {clipped && (
        <button
          type="button"
          className="btn-ghost !py-1 !px-2 text-xs mt-2"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((v) => !v)}
          title={expanded ? "Collapse the description" : "Show the whole description"}
        >
          <ChevronDown className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-180" : ""}`} />
          {expanded ? "Show less" : "Read more"}
        </button>
      )}
    </div>
  );
}
