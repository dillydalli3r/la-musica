import { type ReactNode, useId, useState } from "react";
import { ChevronDown } from "lucide-react";

/** Collapsed preview length, in DISPLAY characters (see `runs`: a link counts
 *  as its label, not as its target). A budget this component owns rather than
 *  a CSS line clamp: the browser appends its own ellipsis wherever the last
 *  clamped line happens to end — "…double platinum.…" — and no stylesheet can
 *  tell it what a word is. Measured this way the cut always lands on a word. */
const PREVIEW_CHARS = 600;

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

/** One run of a line: literal text, or a link. A description comes from a
 *  source over the network and is user-editable, so it is never injected as
 *  HTML — only `http(s)`/`mailto` targets become links and anything malformed
 *  or unsafe stays the literal text it was. */
type Run =
  | { kind: "text"; raw: string }
  | { kind: "link"; label: string; url: string };

/** Split one line into those runs. `label` is what the reader SEES (a link
 *  shows its label, never its target), which is the length the preview budget
 *  counts. */
function runs(text: string): Run[] {
  const out: Run[] = [];
  let last = 0;
  for (const m of text.matchAll(LINK_RE)) {
    const at = m.index;
    const [raw, label, target] = m;
    const url = trimUrl(target ?? raw);
    if (!/^(?:https?:|mailto:)/i.test(url)) continue; // stays literal text below
    // a url straight after `](` belongs to an unterminated `[label](` — leave
    // the whole half-written link as text rather than half-parsing it
    if (!label && text.slice(at - 2, at) === "](") continue;
    if (at > last) out.push({ kind: "text", raw: text.slice(last, at) });
    out.push({ kind: "link", label: label ?? url, url });
    // for a bare url the punctuation trimmed above stays in the text
    last = at + (label ? raw.length : url.length);
  }
  if (last < text.length) out.push({ kind: "text", raw: text.slice(last) });
  return out;
}

/** What one run shows, in characters. */
const displayLen = (r: Run) => (r.kind === "link" ? r.label.length : r.raw.length);

/** The runs as React children: text as-is, links as anchors. */
function nodes(items: Run[]): ReactNode[] {
  return items.map((r, i) =>
    r.kind === "text" ? (
      r.raw
    ) : (
      <a
        key={i}
        href={r.url}
        target="_blank"
        rel="noopener noreferrer"
        title={r.url}
        /* Neutral at rest and one step brighter than the body copy, the accent
           only on hover: the same inline-link idiom as the credits and the
           source rows (TrackDetails). An accent-coloured link is invisible in
           the black & white theme the app defaults to — `accent-soft` IS the
           body colour there — and tints the prose with a hue in all the
           others, which is exactly what a description should not do. */
        className="text-zinc-200 underline decoration-dotted underline-offset-2 break-words hover:text-accent-soft hover:decoration-solid"
      >
        {r.label}
      </a>
    ),
  );
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
 *  article title. Deeper levels step back down.
 *
 *  The margins are the block's own rhythm: a section starts a paragraph clear
 *  of the one above it (`!mt-4`), while the text it introduces sits right
 *  under it (`[&+*]:!mt-1`) — a heading closer to the paragraph before it than
 *  to its own reads as a stray line. `first:!mt-0` leaves the block's own
 *  `mt-2` as the only gap when a description opens with a section. */
const HEADING_CLASS = [
  "text-zinc-100 font-semibold !mt-4 [&+*]:!mt-1 first:!mt-0",
  "text-zinc-200 font-medium !mt-3 [&+*]:!mt-1 first:!mt-0",
  "text-zinc-200 font-medium !mt-3 [&+*]:!mt-1 first:!mt-0",
  "text-zinc-300 font-medium !mt-3 [&+*]:!mt-1 first:!mt-0",
];

type Item =
  | { kind: "p"; runs: Run[] }
  | { kind: "h"; runs: Run[]; level: number };

/** A stored description as its blocks: one per non-empty line — the split this
 *  component always used — with a `== … ==` line becoming a heading. */
function parse(text: string): Item[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = HEADING_RE.exec(line);
      // a marker pair with no title at all (`==  ==`) is not a heading
      return m && m[2].trim()
        ? { kind: "h" as const, runs: runs(m[2]), level: m[1].length }
        : { kind: "p" as const, runs: runs(line) };
    });
}

/** Cut a text run to at most `max` characters, ending at the end of a sentence
 *  when one is within reach and at a word boundary otherwise. "" when not even
 *  one word fits — the caller then leaves the run out instead of showing three
 *  letters and an ellipsis. A preview must never show half a word. */
function trimToBoundary(text: string, max: number): string {
  if (max <= 0) return "";
  const head = text.slice(0, max + 1);
  const sentence = Math.max(
    head.lastIndexOf(". "),
    head.lastIndexOf("! "),
    head.lastIndexOf("? "),
  );
  const at = sentence > max * 0.4 ? sentence + 1 : head.lastIndexOf(" ");
  return at > 0 ? head.slice(0, at).trimEnd() : "";
}

/** What the collapsed block shows: the leading blocks up to `PREVIEW_CHARS`
 *  displayed characters, the block that runs past it cut on a boundary and
 *  closed with the ellipse this component owns. `more` is whether anything was
 *  left out — THAT, and not a clamp that happened to overflow, is what decides
 *  "Read more".
 *
 *  A heading is never cut and never kept without its text: a section title
 *  whose body did not fit is a line that says nothing. */
function preview(items: Item[]): { shown: Item[]; more: boolean } {
  const shown: Item[] = [];
  let budget = PREVIEW_CHARS;
  for (const it of items) {
    const total = it.runs.reduce((n, r) => n + displayLen(r), 0);
    if (total <= budget) {
      shown.push(it);
      budget -= total + 1;      // the paragraph break costs a character
      continue;
    }
    if (it.kind === "h") return { shown, more: true };
    const kept: Run[] = [];
    let used = 0;
    for (const r of it.runs) {
      const len = displayLen(r);
      if (used + len <= budget) {
        kept.push(r);
        used += len;
        continue;
      }
      if (r.kind === "text") {
        const head = trimToBoundary(r.raw, budget - used);
        if (head) kept.push({ kind: "text", raw: head });
      }
      break;
    }
    if (kept.length) {
      shown.push({ kind: "p", runs: [...kept, { kind: "text", raw: "…" }] });
    }
    return { shown, more: true };
  }
  return { shown, more: false };
}

/** A stored description (artist biography / album blurb), collapsed to a
 *  preview and expanded on demand.
 *
 *  Remount with React `key` when the entity being described changes, so a new
 *  artist/album starts collapsed; a react-query refetch of the SAME entity
 *  leaves the component mounted and keeps whatever the reader opened. */
export default function Description({ text }: { text: string }) {
  const bodyId = useId();
  const [expanded, setExpanded] = useState(false);

  // The sources hand over plain text with the original line and paragraph
  // breaks, not HTML: one <p> per paragraph, one heading per `== … ==` line.
  const all = parse(text);
  const { shown, more } = preview(all);
  const body = expanded ? all : shown;

  return (
    <div className="mt-2 text-sm text-zinc-300 leading-relaxed">
      <div id={bodyId} title={`${text.length} characters`} className="space-y-2">
        {body.map((b, i) => {
          if (b.kind === "p") return <p key={i}>{nodes(b.runs)}</p>;
          const depth = Math.min(b.level - 2, HEADING_TAGS.length - 1);
          const Tag = HEADING_TAGS[depth];
          return (
            <Tag key={i} className={HEADING_CLASS[depth]}>
              {nodes(b.runs)}
            </Tag>
          );
        })}
      </div>
      {more && (
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
