/* The MusicBrainz artist page's release-group TYPE derivation — the whole of
 * it, pure and DOM-free, so the way an artist's discography is ordered,
 * bucketed, filtered and folded can be proved without rendering anything.
 *
 * `MBArtistPage` shows ONE discography three ways — the type filter, the
 * collapsible sections, and one "Add to library" row per type — and all three
 * are this module's answer, so a filter can never name a type a section does
 * not draw, or a row offer one the sections hide.
 *
 * A release group's type is the PAIR MusicBrainz reports on every row: one
 * primary type (Album / Single / EP / Broadcast / Other) plus any number of
 * secondary ones (Live / Compilation / Soundtrack / …). Its name here is
 * MusicBrainz's own compound spelling — "Album + Live", primary type first —
 * which is also the type SELECTION the server is asked for, so one label means
 * one thing on both sides of the wire.
 *
 * Three decisions live here:
 *
 *   * RANK — Album, EP and Single lead, in that order, and everything else
 *     follows. It used to be the order MusicBrainz happened to serve, which
 *     made an artist with 49 live albums and 10 studio ones open on
 *     "Album + Live": a discography that reads as live records. A compound
 *     label is ranked by its PRIMARY type ("Album + Live" sorts with Albums,
 *     "EP + Compilation" with EPs), which is also the part of the type a
 *     listener means when they say "the albums". Within a rank the type with
 *     the most release groups leads, then the label — so the order is total
 *     and does not depend on which page of MusicBrainz the rows arrived in.
 *
 *   * SECTIONS — those three leading types get a section each, and EVERY other
 *     type is folded into one "More" bucket, ordered by how many release groups
 *     it holds. An artist's long tail is usually many types of one or two
 *     groups each (a broadcast, a DJ-mix, a demo); printing one chip and one
 *     block per type made a phone-width page of chips and a discography nobody
 *     scrolls. The bucket keeps the page to four headers, and the filter menu
 *     (below) still reaches every single type by name.
 *
 *   * COLLAPSING — the leading sections start open (they are what a reader
 *     came for) and the "More" bucket starts folded away. A type the reader
 *     NAMED — paged to it in the filter menu — forces its own section open, so
 *     a search can never land on a section that has folded its rows away.
 *
 * The filter menu's own text filter is `filterTypeOptions`: a case- and
 * spacing-insensitive match over the type LABELS, so "live" finds
 * "Album + Live" and "album+live" finds the same row. */

/** The release-group fields the grouping reads — nothing else, so a caller can
 *  hand in its own row type (`MBReleaseGroupRow` does). */
export interface TypeRow {
  primary_type?: string | null;
  secondary_types?: string[] | null;
}

/** One compound type and the rows MusicBrainz filed under it. */
export interface TypeGroup<T> {
  label: string;
  list: T[];
}

/** What the page draws, and what the filter menu offers: a leading primary
 *  type with every compound label under it, or the folded-away long tail. */
export interface TypeSection<T> {
  /** What the header and the filter menu call it: MusicBrainz's own primary
   *  type ("Album", "EP"), or `EXTRAS_LABEL` for the bucket. */
  label: string;
  /** The lower-cased primary type this section leads with, or "" for the
   *  bucket of everything that does not lead. */
  primary: string;
  /** True for the bucket — the one section that starts folded away. */
  extras: boolean;
  /** The compound labels this section holds, in rank/count/label order. */
  groups: TypeGroup<T>[];
  /** Rows in the whole section, across its compound labels. */
  count: number;
}

/** One row of the filter menu: a section or a compound label inside it. */
export interface TypeFilterOption {
  /** The full compound label (or the section's), i.e. what a pick selects. */
  label: string;
  count: number;
  /** The section this row belongs to, for the menu's indentation. */
  section: string;
  /** True when the row is a compound label under a section ("Album + Live"),
   *  false when the row IS the section ("Album"). */
  compound: boolean;
}

/** The three types that make up almost every artist's real discography, in the
 *  order a reader looks for them. Everything else follows them. */
export const RG_TYPE_FIRST = ["album", "ep", "single"] as const;

/** The pick that means "no type filter". */
export const ALL_TYPES = "All";

/** The bucket the long tail is folded into. */
export const EXTRAS_LABEL = "More";

/** MusicBrainz's own spelling of a group's type: the primary type first, then
 *  every secondary type ("Album + Live"). A row MusicBrainz served without a
 *  primary type is "Other" — the same default the sections bucket it under. */
export function rgTypeLabel(rg: TypeRow): string {
  return [rg.primary_type || "Other", ...(rg.secondary_types ?? [])].join(" + ");
}

/** The primary half of a compound label — what a listener means by "the
 *  albums" when they say "the Album + Live ones". */
export function rgTypePrimary(label: string): string {
  return label.split(" + ")[0].trim();
}

/** 0, 1, 2 for Album, EP and Single; 3 (one past the leaders) for everything
 *  else — so a plain ascending sort puts the leaders first. */
export function rgTypeRank(label: string): number {
  const i = (RG_TYPE_FIRST as readonly string[]).indexOf(rgTypePrimary(label).toLowerCase());
  return i === -1 ? RG_TYPE_FIRST.length : i;
}

/** The artist's discography grouped by COMPOUND type label, in the page's
 *  order: the three leading types, then the types carrying the most rows, then
 *  the label — a total order, so two pages of MusicBrainz rows cannot shuffle
 *  it. */
export function byReleaseGroupType<T extends TypeRow>(groups: T[]): TypeGroup<T>[] {
  const order: string[] = [];
  const byLabel = new Map<string, T[]>();
  for (const rg of groups) {
    const label = rgTypeLabel(rg);
    const list = byLabel.get(label);
    if (list) list.push(rg);
    else {
      byLabel.set(label, [rg]);
      order.push(label);
    }
  }
  return order
    .map((label) => ({ label, list: byLabel.get(label) as T[] }))
    .sort((a, b) => rgTypeRank(a.label) - rgTypeRank(b.label)
      || b.list.length - a.list.length
      || a.label.localeCompare(b.label));
}

/** The page's sections: Album, EP and Single each leading their own compound
 *  labels, and everything else in one `EXTRAS_LABEL` bucket ordered by how many
 *  release groups it holds. Sections only exist where the artist has rows, so a
 *  discography of nothing but albums draws one header, not four.
 *
 *  The bucket's label is the bucket's, and the compound labels inside it keep
 *  their MusicBrainz spelling — a reader who opens it still reads "Other +
 *  Compilation", never "Misc". */
export function typeSections<T extends TypeRow>(groups: T[]): TypeSection<T>[] {
  const ranked = byReleaseGroupType(groups);
  const sections: TypeSection<T>[] = [];
  for (const group of ranked) {
    const rank = rgTypeRank(group.label);
    const leading = rank < RG_TYPE_FIRST.length;
    // Back to MusicBrainz's own spelling for the header (the rank compares
    // lower-cased; the row says "EP", not "ep").
    const primary = leading ? rgTypePrimary(group.label) : "";
    const label = leading ? primary : EXTRAS_LABEL;
    let section = sections.find((s) => s.label === label);
    if (!section) {
      section = { label, primary: primary.toLowerCase(), extras: !leading, groups: [], count: 0 };
      sections.push(section);
    }
    section.groups.push(group);
    section.count += group.list.length;
  }
  // The bucket is the tail of a discography that HAS leaders. An artist whose
  // every type is an "other" one has no tail — the bucket is the whole
  // discography — and a fold over it would hide every row behind a press that
  // nothing on the page explains, so it starts open like any other section.
  if (sections.length === 1 && sections[0].extras) sections[0].extras = false;
  // Otherwise the bucket is last whatever its size: the leaders are the reading
  // order, and a folded bucket between EP and Single would bury them.
  return sections.sort((a, b) => Number(a.extras) - Number(b.extras));
}

/** Does a section start open? The leading types do — they are what a reader
 *  opens the page for — and the folded bucket does not. */
export function typeSectionDefaultOpen<T>(section: TypeSection<T>): boolean {
  return !section.extras;
}

/** Is this section's body showing?
 *
 *  `opened` is the reader's own override, keyed by section label, and it always
 *  wins: a section folded by hand stays folded. Otherwise a type the reader
 *  NAMED — the filter menu's pick — opens its section (a search must not land
 *  on a section whose rows are folded away), and everything else falls back to
 *  `typeSectionDefaultOpen`. */
export function typeSectionOpen<T>(
  section: TypeSection<T>,
  opened: Record<string, boolean>,
  selected: string = ALL_TYPES,
): boolean {
  const mine = opened[section.label];
  if (mine !== undefined) return mine;
  if (selected !== ALL_TYPES
    && (selected === section.label || section.groups.some((g) => g.label === selected))) {
    return true;
  }
  return typeSectionDefaultOpen(section);
}

/** One press on a section header: the override that flips exactly this
 *  section, leaving every other fold where the reader left it. */
export function toggleTypeSection<T>(
  opened: Record<string, boolean>,
  section: TypeSection<T>,
): Record<string, boolean> {
  return { ...opened, [section.label]: !typeSectionOpen(section, opened) };
}

/** The sections a pick leaves: "All" is the whole discography; a section label
 *  is every compound type under it; a compound label is exactly that type —
 *  never every group of its primary (selecting "Album + Live" is the live
 *  albums, not every album). A label nothing matches leaves nothing. */
export function selectedSections<T>(
  sections: TypeSection<T>[],
  selected: string,
): TypeSection<T>[] {
  if (selected === ALL_TYPES) return sections;
  const out: TypeSection<T>[] = [];
  for (const section of sections) {
    if (section.label === selected) {
      out.push(section);
      continue;
    }
    const groups = section.groups.filter((g) => g.label === selected);
    if (groups.length) {
      // The section's own header still names where the rows live, but its
      // count is the filtered one — the header never claims rows it is not
      // drawing.
      out.push({
        ...section,
        groups,
        count: groups.reduce((n, g) => n + g.list.length, 0),
      });
    }
  }
  return out;
}

/** Every row the filter menu offers, in the order the page draws them: "All"
 *  is the caller's (it owns the discography total), then each section, then the
 *  compound labels inside it that a section row does not already name. */
export function typeFilterOptions<T>(sections: TypeSection<T>[]): TypeFilterOption[] {
  const out: TypeFilterOption[] = [];
  for (const section of sections) {
    out.push({ label: section.label, count: section.count, section: section.label, compound: false });
    for (const group of section.groups) {
      if (group.label === section.label) continue;   // the section row IS this label
      out.push({
        label: group.label,
        count: group.list.length,
        section: section.label,
        compound: true,
      });
    }
  }
  return out;
}

/** Case- and spacing-insensitive spelling of a label or a query: the menu is
 *  typed by hand, so "album+live", "Album +Live" and "ALBUM + LIVE" are one
 *  query, and they all reach the compound label MusicBrainz spells
 *  "Album + Live". */
function foldTypeText(s: string): string {
  return s.toLowerCase().replace(/\s*\+\s*/g, " + ").replace(/\s+/g, " ").trim();
}

/** The filter menu's text filter over the type LABELS: an empty query is every
 *  row, otherwise the rows whose label contains the query. Matching the label
 *  and nothing else is the point — the menu is a way to reach a type by name,
 *  not a second search over the discography. */
export function filterTypeOptions(
  options: TypeFilterOption[],
  query: string,
): TypeFilterOption[] {
  const needle = foldTypeText(query);
  if (!needle) return options;
  return options.filter((o) => foldTypeText(o.label).includes(needle));
}
