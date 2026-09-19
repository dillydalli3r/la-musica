/** The tag registry: the client's single accessor for what the app knows
 *  about a tag.
 *
 *  Every fact it serves is derived, server side, from the code that owns it
 *  (see server/tags_registry.py): the vocabulary from mlo.audio.TAG_MAP, the
 *  writer's name from the script table, the checks from the grader's own
 *  tables, and the excess verdict from mlo.grader.tag_key_allowed. So the
 *  track editor, the bulk dialog and the Grading page read one answer instead
 *  of each keeping a hand-written list that can drift from the engine.
 *
 *  Fetched once per session and cached forever (react-query, staleTime
 *  Infinity): it changes only when the server's own code changes. */
import { useQuery } from "@tanstack/react-query";
import { getToken, serverUrl } from "../api";

/** A tag's write gate: the config family the per-filetype matrix keys on, the
 *  master switches that gate that family, and what
 *  `should_write_audio_tag(..., filetype)` answers for each container with
 *  the shipped defaults. */
export interface TagWriteGate {
  family: string | null;
  filetypes: Record<string, boolean>;
  switches: string[];
}

export interface TagInfo {
  key: string;
  label: string;
  meaning: string;
  family: string;
  writer: string;
  /** The grade_check_* keys that grade this tag. */
  graded_by: string[];
  /** The issue codes the grader reports AGAINST this tag (what the track page
   *  marks a value with — the grader's own verdict, not a re-derivation). */
  issue_codes: string[];
  write_gate: TagWriteGate;
  /** The closed value set the code validates against, when it has one. */
  enum: string[] | null;
}

export interface CheckInfo {
  key: string;
  label: string;
  default: boolean;
  /** The tags this check grades — the inverse of each tag's graded_by. */
  tags: string[];
}

export interface TagRegistry {
  families: { id: string; label: string }[];
  tags: TagInfo[];
  /** Folded emitted spelling -> canonical key (foldSpelling below). */
  aliases: Record<string, string>;
  /** The grader's own allow-list (mlo.grader.TAG_ALLOWLIST), already folded:
   *  a name in here, in aliases or under allowed_prefixes is a name the strip
   *  passes keep — anything else is what the grader calls EXCESS. */
  allowed: string[];
  allowed_prefixes: string[];
  checks: CheckInfo[];
}

export const TAG_REGISTRY_KEY = ["tag-registry"] as const;

async function fetchTagRegistry(): Promise<TagRegistry> {
  // Through `serverUrl()` + the session token rather than a bare relative
  // path: the Tauri phone/desktop shells serve the UI from tauri://localhost
  // and talk to a backend on another address.
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(`${serverUrl()}/api/tags/registry`, {
    credentials: "include",
    headers,
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`tag registry: HTTP ${r.status}`);
  return (await r.json()) as TagRegistry;
}

/** The registry, or undefined while it loads. Pages fall back to the raw tag
 *  key until it arrives — never to a second copy of a label. */
export function useTagRegistry(): TagRegistry | undefined {
  const { data } = useQuery({
    queryKey: TAG_REGISTRY_KEY,
    queryFn: fetchTagRegistry,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
  });
  return data;
}

const fold = (v: unknown) => String(v ?? "").trim().toLowerCase();

/** A file-side tag name, folded the way the grader compares names.
 *
 *  The twin of `mlo.grader._tag_key_norm` plus that predicate's wrapper rule:
 *  the name inside a "TXXX:desc" / "----:vendor:name" wrapper is what gets
 *  compared, and case, spaces and underscores are dropped. The registry's
 *  alias index and allow-list are shipped already folded, so this is the only
 *  piece of the comparison that has to exist on this side. */
export function foldSpelling(tag: string): string {
  const bare = tag.includes(":") ? tag.slice(tag.lastIndexOf(":") + 1) : tag;
  return bare.replace(/[\s_]+/g, "").toUpperCase();
}

/** The registry entry a file-side tag name belongs to — by its folded
 *  spelling, so "encoder_quality" and the "ENCODER_QUALITY" this app writes
 *  resolve to the same tag.
 *
 *  A lyrics transform carries its language in the NAME (TRANSLATION-EN,
 *  TRANSLITERATION-JA-LATN), so those resolve to the base tag's entry — the
 *  registry has one row for the family and the language is part of the row's
 *  value, not a separate tag. */
export function tagInfoOf(reg: TagRegistry | undefined, tag: string): TagInfo | undefined {
  if (!reg) return undefined;
  const folded = foldSpelling(tag);
  const base = reg.allowed_prefixes.some((p) => tag.toUpperCase().startsWith(p))
    ? folded.replace(/-.*$/, "")
    : folded;
  const key = reg.aliases[folded] ?? reg.aliases[base];
  return key ? reg.tags.find((t) => t.key === key) : undefined;
}

/** The label to show for a tag name — the registry's when it knows the tag,
 *  the raw name otherwise (an unknown tag has no label to invent). */
export function tagLabel(reg: TagRegistry | undefined, tag: string): string {
  return tagInfoOf(reg, tag)?.label ?? tag;
}

/** Whether the grader would call this tag EXCESS — i.e. whether the strip
 *  pass (Optimize / Format all) would remove it.
 *
 *  The sets it tests against are the ones the SERVER derived from
 *  `mlo.grader.tag_key_allowed` and its own allow-list, so this is membership
 *  plus the two lyrics-transform prefixes and nothing else. */
export function isExcessTag(reg: TagRegistry | undefined, tag: string): boolean {
  if (!reg) return false;
  const folded = foldSpelling(tag);
  if (reg.aliases[folded] || reg.allowed.includes(folded)) return false;
  const upper = tag.toUpperCase();
  return !reg.allowed_prefixes.some((p) => upper.startsWith(p));
}

/** The grade checks this tag's value fails, by the grader's own issue codes.
 *  `issues` is the track's payload list — the same codes the grade badge
 *  counts, so the row and the badge can never disagree. */
export function failedChecksOf(
  reg: TagRegistry | undefined,
  tag: string,
  issues: string[],
): string[] {
  const info = tagInfoOf(reg, tag);
  if (!info || !info.issue_codes.length) return [];
  const hit = new Set(issues.map((i) => i.toUpperCase()));
  return info.issue_codes.filter((code) => hit.has(code.toUpperCase()));
}

/** The reason a stored value fails its own tag's rule, or null.
 *
 *  Only closed value sets the registry carries are judged here (`enum`): a
 *  MOOD outside the eight the classifier writes, an advisory that is not
 *  0/1/2, a MEDIA the grader does not know. GENRE is deliberately NOT judged
 *  this way — its vocabulary is 2 200 MusicBrainz names, and the grader
 *  already names a bad one in its GENRE_VOCAB issue, which
 *  `failedChecksOf` surfaces instead. */
export function invalidValueReason(
  reg: TagRegistry | undefined,
  tag: string,
  value: unknown,
): string | null {
  const info = tagInfoOf(reg, tag);
  if (!info?.enum) return null;
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  const known = new Set(info.enum.map(fold));
  const bad = raw.split(/\s*[/;]\s*/).map(fold).filter((v) => v && !known.has(v));
  if (!bad.length) return null;
  return `${info.label} is not one of ${info.enum.join(" / ")}`;
}

/** Everything a tooltip says about one tag, from the registry alone. */
export function tagTooltip(reg: TagRegistry | undefined, tag: string): string {
  const info = tagInfoOf(reg, tag);
  if (!info) return tag;
  const lines = [`${info.label} (${info.key})`];
  if (info.meaning) lines.push(info.meaning);
  lines.push(`Written by: ${info.writer}`);
  const checks = info.graded_by
    .map((k) => reg?.checks.find((c) => c.key === k)?.label ?? k);
  lines.push(checks.length ? `Graded by: ${checks.join(", ")}` : "Not graded");
  if (info.write_gate.family) {
    const gate = [`its "${info.write_gate.family}" family in Tagging settings`];
    if (info.write_gate.switches.length) gate.push(...info.write_gate.switches);
    lines.push(`Writes gated by: ${gate.join(", ")}`);
  }
  return lines.join("\n");
}
