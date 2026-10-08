/** Column defs, visibility/width prefs, and the columns chooser + resizer.
 *
 * Imported by the library/album/cached/trash tables and their tracklists
 * (see AlbumPage, LibraryPage, ExportPage, TrashPage).
 * Column visibility and widths persist per view in localStorage. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Columns3, X } from "lucide-react";
import type { MouseEvent as ReactMouseEvent } from "react";

/** One table column definition: id for prefs/widths, header label, sort key.
 *  defHidden columns exist (sortable, toggleable via the columns chooser)
 *  but are not part of the default visible set.
 *  `tag` marks a user-added column backed by that tag (see useCustomColumns);
 *  the columns chooser offers a remove control for exactly those. */
export interface Col {
  id: string;
  label: string;
  sortKey: string;
  defHidden?: boolean;
  tag?: string;
  /** Furniture rather than data: the row number and the cover. What a table
   *  draws when every DATA column is gone — which is why a stored visible list
   *  holding nothing else is not obeyed (`useColumnPrefs`). */
  chrome?: boolean;
}

/** A user-added column: one file tag shown as its own table column. */
export interface CustomCol {
  id: string;
  label: string;
  tag: string;
}

/** The reader's own column state for one view, stored BESIDE the visible-id
 *  list (`mlo-coldft-*`) and read as the state — see `useColumnPrefs`.
 *
 *  `removed` is what they took out of the columns this build SHIPS visible;
 *  `added` is what they put there that it does not ship (the `defHidden`
 *  built-ins, and their own tag columns). The record says what was CHOSEN,
 *  where a stored list only says what was once drawn — which is how a list of
 *  three ids, written by a build with fewer album columns, could pass for a
 *  reader who had unticked four of seven. `v` is what makes that impossible: a
 *  record without the version is unknown, and unknown is reconciled once from
 *  whatever the list still has (see the hook). */
interface ColRecord {
  v: 2;
  removed: string[];
  added: string[];
}

/** Column layout shared by every album tracklist — the album page table and
 *  the expanded album rows in the library albums view are the same table, so
 *  visible columns and drag-resized widths are stored under one prefs key.
 *  Every column carries a px floor: a fixed layout only honours WIDTHS, so a
 *  floor expressed as a percentage of the table moves with the table, and one
 *  expressed as `auto` is handed whatever the columns with widths leave — the
 *  measured 0 px name at 342. The floors are `md:`-scoped where the phone fold
 *  already drops the column (see ALBUM_TRACK_PHONE_CLS), and the floor that
 *  sums them into the table's own is `ALBUM_TRACK_MIN_W`. */
export const ALBUM_TRACK_COL_W: Record<string, string> = {
  num: "w-16",
  cover: "w-[52px]",
  // The name column is the row's one flexible column: `auto`, so the fixed
  // layout hands it the width the columns with floors leave instead of growing
  // EVERY column by the same proportion (measured at 1440: the name took 378 of
  // 1200 while its own floor is 280, and the leftover of the other eleven
  // columns sat under their own text). The owner's album page wrapped a long
  // title to two lines with 900 px of free table width beside it — the cell's
  // link is what is left of the column after the row's own chrome, so the
  // column has to take the free width for the title to get it.
  //
  // The floor it needs — 280, what this cell's parts need on ONE line: the name
  // plus the marks that belong to it (advisory, cached, video, the issue
  // counter) plus the row's constant trailing slot (the trailing controls),
  // measured 283 px on the widest row of a real album — is carried INTO the
  // cell by `ColFloorHolder` instead of by a width here, because a width is
  // exactly what stops the column absorbing the free width. It has to be a
  // floor the cell reports: the table's floor follows its columns' own widths
  // (TABLE_FIT), an auto column is measured from its CONTENT, so without this
  // box a name shorter than the floor would leave the column narrower than the
  // row's own chrome needs and the name would be crushed (measured, a 66 px
  // column with a 0 px name wrapped nine lines deep).
  // Below `md` the column is auto on purpose and takes whatever the phone fold
  // leaves: the measured 122 px at 390 is a readable column, while a 280 px
  // floor there would put a 280 px column in a 342 px row (which is why the
  // holder is `hidden` there — see ColFloorHolder).
  title: "md:w-auto",
  // 208, measured the same way as every other floor here — "widest value + a
  // few px of slack" — against the two-name VALUES the app's own vocabulary
  // writes, which is what a genre cell shows: "Metal; Alternative Metal" is
  // 178 px at this table's font, "Metal; Progressive Metal" 184, and the old
  // 160 (sized to "Rock; Garage Rock", 148) wrapped the owner's own album —
  // every row two lines tall (measured 60 px against the clean row's 52) for
  // a value that fits as soon as the column has 184 px of content box. 208
  // leaves the 24 px gutter and 6 px of slack, and the floor the table sums
  // still fits a 1024 px window (1000 px of columns). A longer pair — "Hip
  // Hop; East Coast Hip Hop" is 210 — and a three-name value (302) still wrap,
  // and the row still grows with them: a taller row is honest, but only for a
  // value that really is long. (The Library's own Tracks table keeps 96: it
  // CLIPS with the whole value in the cell's title — cell-ellipsis — which is
  // the shape a dense table may use, and it is the one table that cannot
  // afford the width: measured, its columns are 1537 px in a 1200 px box at a
  // 1440 window, so its wrapper already scrolls.)
  genre: "md:w-52",
  dur: "w-20",
  bitrate: "md:w-[184px]",
  dr: "md:w-14",
};
/** Floor for a table whose columns carry their own width (`_COL_W` maps): a
 *  fixed layout squares up to `w-full` by scaling every column down and
 *  handing the auto column whatever is left, so a table too narrow for its
 *  columns renders the name column one character per line — the library's
 *  track table measured a 0 px title at 580 px wide.
 *
 *  `md:w-max` + `md:min-w-full` is that floor: as wide as the table's own
 *  columns need, never narrower than the box it sits in, so a table whose
 *  columns do not fit stops at its own width and the `overflow-x-auto` wrapper
 *  scrolls from there instead. `table-fit` is the marker index.css needs to
 *  read that width off the HEADER row's cells — see the `table-fit` block
 *  there, and the measurements below.
 *
 *  It was `md:min-w-max` — the same rule, but asked of the engine as an
 *  intrinsic MINIMUM. Measured, that form is a Blink-only behaviour: a table
 *  with `width: 100%` and `min-width: max-content` on a fixed layout came out
 *  738 px in Chromium (the floor), **17,895,698 px in Firefox** — Gecko's
 *  unconstrained sentinel (2^30 app units) leaking out of its intrinsic pass,
 *  whatever the columns or the content are, fixed by nothing on the wrapper or
 *  the cells (issue #71: every album page on Gecko, and with it every table
 *  here) — and in WebKit it was not honoured at all: the album tracklist's
 *  name column measured 188 px where its floor is 280, and 0 px at a 620 px
 *  box, which is the crushed name this floor exists to prevent.
 *  `width: max-content; min-width: 100%` is the same floor computed the same
 *  way by all three engines (measured 738/738/738 at a 620 px box, 1400 at
 *  1400), so the floor is stated as a width instead of as a minimum.
 *
 *  `md:` only, because below it the phone fold has already dropped the columns
 *  that do not fit and the couple left share the width with room to spare —
 *  same as before this floor existed. */
export const TABLE_FIT = "table-fit w-full md:w-max md:min-w-full";

/** The album tracklist's floor: the same column-driven rule every other table
 *  here uses (`TABLE_FIT`), not a number picked by hand.
 *
 *  It used to be `md:min-w-[814px]`, derived from the columns as they then
 *  were: the fixed ones plus the corner control took 272 px up front, the
 *  percentage ones took 42% of what was left, the auto title got the
 *  remainder, and 814 was the width where that title was still 200 px. Every
 *  part of that arithmetic was a snapshot — add a column (a user's tag column,
 *  a second disc's cover), fold one on a phone, resize one by its own handle,
 *  and the constant no longer described the table it was pinned to. It only
 *  ever applied from `md` up anyway (below it the fold leaves the couple of
 *  columns a phone can use, and a floor there is what put an 814 px table
 *  inside a 342 px screen).
 *
 *  What is left of that: every album-tracklist column carries its width
 *  (`ALBUM_TRACK_COL_W`, plus the corner control's own class), and this is the
 *  width-based form of `TABLE_FIT` — the table stops at its own columns'
 *  width and lets its `overflow-x-auto` wrapper scroll. `md:min-w-max` said
 *  the same thing but only Chromium implemented it (Gecko saturated it to
 *  17,895,698 px, WebKit never honoured it — see TABLE_FIT for the
 *  measurements), so the floor is a width now, read off the header row with
 *  the help of `table-fit` (index.css).
 *
 *  The name column is `md:w-auto` on purpose: an auto column contributes
 *  nothing to a width, so without the width the cell reports through
 *  `ColFloorHolder` the one column that must never collapse — it was measured
 *  at 0 px — would be the one the floor forgot. */
export const ALBUM_TRACK_MIN_W = "table-fit md:w-max md:min-w-full";

/** Floor for a user-added tag column (`tag:*` ids): the values are free text,
 *  so it gets a readable minimum — the width a single genre name holds (the
 *  genre column itself is sized to a two-name value, `ALBUM_TRACK_COL_W`).
 *  Without a width of its own such a column is auto, and TABLE_FIT's
 *  `md:w-max` floor would then grow the table to the longest tag value it can
 *  find. */
export const TAG_COL_W = "w-[96px]";

/** Default album-tracklist columns (num/cover/title/genre/dur/bitrate/DR). */
export const ALBUM_TRACK_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "tracknumber", chrome: true },
  { id: "cover", label: "", sortKey: "", chrome: true },
  { id: "title", label: "Title", sortKey: "tags.TITLE" },
  { id: "genre", label: "Genre", sortKey: "tags.GENRE" },
  { id: "dur", label: "Dur", sortKey: "tech.length" },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate" },
  { id: "dr", label: "DR", sortKey: "tags.DYNAMIC RANGE" },
];
/** A phone (390 px) table keeps the row's own name and drops the numbers: at
 *  that width a row that keeps them squeezes the name to nothing, and the
 *  table's own scroll wrapper cannot give it back.
 *
 *  The class has to sit on the header AND on the cells, or the fixed-layout
 *  grid misaligns; `md` is where each column comes back. */
export const PHONE_HIDE = " hidden md:table-cell";

/** The album tracklist's own phone fold — deliberately NOT `TRACK_PHONE_CLS`.
 *  An album's tracklist is read as a spine: the track NUMBER is the row's
 *  address in the release and the LENGTH beside it is what a reader scans down,
 *  so both stay on a phone. The Library's Tracks view lists unrelated files,
 *  has no spine to keep, and folds them there; the album page does not.
 *
 *  What a phone cannot use is the reference data — genre, bitrate, dynamic
 *  range, and every user-added tag column (`phoneHide` folds those, the same
 *  fallback the other tables use). The COVER folds with them, and that is
 *  measured rather than stylistic: the page hands the table 342 px at 390 px,
 *  the corner control (columns chooser + select toggle) takes 76 and # + Dur
 *  another 144, so the Title column gets the last 122 — and the title cell has
 *  to hold the trailing controls beside the name too. The 52
 *  px cover is what buys the name its room, and it is the one column this table
 *  can lose without costing the reader anything: the album's own cover is the
 *  hero above the table, and it is the art every track in the album shares. */
export const ALBUM_TRACK_PHONE_CLS: Record<string, string> = {
  cover: PHONE_HIDE, genre: PHONE_HIDE, bitrate: PHONE_HIDE, dr: PHONE_HIDE,
};

/** The track table's floors — one narrowest-usable width per column, in px,
 *  same rule as the album table's own map: they are the columns the table's
 *  floor (`TABLE_FIT`) is measured from, so a window wider than their sum
 *  shares the extra out with the flexible name column.
 *
 *  Shared by every table that lists whole tracks in library order: the
 *  Library's Tracks view and the Export page's preview. They were two tables
 *  with two sets of numbers until the export preview's `#` column was 32 px
 *  wide — which is 8 px of content box after `td`'s px-3 padding, so a
 *  two-digit track number wrapped onto two lines. */
export const TRACK_COL_W: Record<string, string> = {
  // 64 px, not the 48 a single digit suggests: 24 px of the column is the
  // cell's own gutter, and what is left has to hold the widest number a row
  // can show. Library rows print a track number ("10"), and a row without one
  // is numbered by its position in the list ("200" in a full-library preview).
  num: "w-16",
  cover: "w-[52px]",
  // 240, measured — and measured against this cell's own parts rather than
  // picked: the name plus the marks that belong to it (advisory, grade,
  // cached, video) plus the row's trailing controls are 237 px on the widest
  // row of a real library, and the floors here are "widest value + a few px of
  // slack" everywhere else. It is carried by `ColFloorHolder` inside the cell,
  // not by a width here, because the column is `auto`: the name is the row's
  // one flexible column, and it must absorb the table's free width rather than
  // let the fixed layout grow every column of the row by the same proportion
  // (see ALBUM_TRACK_COL_W.title). The floor still has to be REPORTED by the
  // cell, or the table's floor looks past it and the fixed layout hands the
  // name 0 px. What this replaced was 280: the same measurement taken
  // once, rounded up, and then paid for by every other column — the table's
  // floor (`TABLE_FIT`) follows the columns' own widths, so 40 px of headroom
  // here pushed the whole table 40 px wider than its data and put a horizontal
  // scroll under windows that would otherwise have fitted.
  // `md:` like the album table's name column: on a phone the title is the only
  // column left beside the cover, so it takes the whole row instead.
  title: "md:w-auto",
  // 120, measured: "Artist Gamma" at the table's own font is 112 px wide, so
  // the old 108 broke the name across two lines inside a column whose whole
  // job is saying who the track is by. The floors below are sized the same way
  // (widest value + a few px of slack), which is what keeps a row one line tall
  // instead of three.
  artist: "w-[120px]",
  album: "w-[112px]",
  year: "w-16",
  genre: "w-24",
  // The MediumChip's own longest label ("Digital Media") is 108 px wide.
  media: "w-[112px]",
  // 88 px: an hour-plus length is seven characters ("1:02:33"), which the old
  // 64 px floor could only break onto a second line — and the header is what
  // sets the floor here, because a floor has to clear TWO things: "Duration"
  // plus its sort arrow is 86 px of nowrap label, and a fixed-layout column
  // narrower than that paints the label over the column beside it (the same
  // rule ALBUM_COL_W documents for ADR). The album tracklist's own length
  // column keeps 80: its label is the three letters "Dur".
  duration: "w-[88px]",
  // 184, measured: `fmtTech`'s own string ("FLAC 16/44.1 · 104 kbps") is 177 px
  // — the app's precedent for this column is the duration one above, sized to
  // its widest value, and a format/bitrate readout that breaks into three lines
  // (one of them empty) is what the old 88 px floor did.
  bitrate: "w-[184px]",
  // 56, not 48: the "DR" label plus its sort arrow is 52 px of nowrap header,
  // which a 48 px column paints over its neighbour (the same rule the albums
  // table's "ADR" column follows).
  dr: "w-14",
  source: "w-20",
  type: "w-20",
  inst: "w-20",
  composer: "w-[112px]",
  lyricist: "w-[112px]",
  remixer: "w-[96px]",
  // The tracklist's own id for the same length column the Tracks view calls
  // `duration` (see TRACK_PHONE_CLS).
  dur: "w-20",
};

/** The floor an `auto` title column carries INTO its cell — the width of the
 *  zero-height box `ColFloorHolder` draws, per table:
 *
 *   * `ALBUM_TRACK_TITLE_FLOOR` — the album tracklist's 280 px floor
 *     (ALBUM_TRACK_COL_W.title) minus the 24 px of gutter `.th`/`.td` put
 *     inside every cell, which is the border-box width the fixed layout reads
 *     off the cell's content.
 *   * `TRACK_TITLE_FLOOR` — the same for TRACK_COL_W.title's 240 px.
 *
 *  They are the ONE part of the name column that did not move into a class of
 *  its own: the number lives in the comment above each map entry, and the
 *  holder's `w-[…]` literals are what Tailwind can see (a computed class name
 *  is never generated). Change a name floor in the maps above and this pair
 *  changes with it — the check that measures the tables' floors fails if it
 *  does not. */
export const ALBUM_TRACK_TITLE_FLOOR = "w-[256px]";
export const TRACK_TITLE_FLOOR = "w-[216px]";

/** The floor-holder box. The name column has to be `auto` (it is the row's one
 *  flexible column — see ALBUM_TRACK_COL_W.title), and a `table-layout: fixed`
 *  table reads an auto column's floor off its CONTENT: a `min-width` on the
 *  cell itself is ignored there (measured: a title cell asking for 280 px came
 *  out 66 px wide, and its name 0 px), while this empty zero-height box carries
 *  the floor's width into the cell's own content size. `hidden` below `md`,
 *  where no floor applies and the phone's own fold gives the name whatever is
 *  left. Drawn in the header cell — that is enough, since the floor takes the
 *  widest content of the column's cells and the header is one of them. */
export function ColFloorHolder({ className }: { className: string }) {
  return <span aria-hidden="true" className={`hidden md:block h-0 ${className}`} />;
}

/** The track table's columns, in render order. */
export const TRACK_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "tracknumber", chrome: true },
  { id: "cover", label: "", sortKey: "", chrome: true },
  { id: "title", label: "Title", sortKey: "tags.TITLE" },
  { id: "artist", label: "Artist", sortKey: "artist" },
  { id: "album", label: "Album", sortKey: "album" },
  { id: "year", label: "Year", sortKey: "tags.DATE" },
  { id: "genre", label: "Genre", sortKey: "tags.GENRE" },
  { id: "media", label: "Media", sortKey: "tags.MEDIA" },
  { id: "duration", label: "Duration", sortKey: "tech.length" },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate" },
  // ReplayGain deliberately has NO column: it is playback metadata — a
  // compatible player applies it to keep loudness even between tracks. Only
  // Dynamic Range is shown.
  { id: "dr", label: "DR", sortKey: "tags.DYNAMIC RANGE" },
  { id: "source", label: "Source", sortKey: "tags.SOURCE" },
  { id: "type", label: "Type", sortKey: "is_video" },
  { id: "inst", label: "INST", sortKey: "tags.INSTRUMENTAL" },
  { id: "composer", label: "Composer", sortKey: "tags.COMPOSER", defHidden: true },
  { id: "lyricist", label: "Lyricist", sortKey: "tags.LYRICIST", defHidden: true },
  { id: "remixer", label: "Remixer", sortKey: "tags.REMIXER", defHidden: true },
];

/** The track tables that list whole tracks in LIBRARY order — the Tracks view,
 *  the export preview, and the Library's expanded album rows (which share the
 *  album columns but fold like a track list): only the cover and the title stay
 *  on a phone, every column id named here folds at `md`. The tables name the
 *  length column differently (`duration` in the Tracks view, `dur` in an album
 *  tracklist), which is why both ids appear. The album PAGE's tracklist keeps
 *  `#` and `dur` and is folded by ALBUM_TRACK_PHONE_CLS instead — see there. */
export const TRACK_PHONE_CLS: Record<string, string> = {
  num: PHONE_HIDE, artist: PHONE_HIDE, album: PHONE_HIDE, year: PHONE_HIDE,
  genre: PHONE_HIDE, media: PHONE_HIDE, duration: PHONE_HIDE, dur: PHONE_HIDE,
  bitrate: PHONE_HIDE, dr: PHONE_HIDE, source: PHONE_HIDE, type: PHONE_HIDE,
  inst: PHONE_HIDE, composer: PHONE_HIDE, lyricist: PHONE_HIDE, remixer: PHONE_HIDE,
};

/** Tag columns the user added fold with the built-ins they sit beside. */
export function phoneHide(cls: Record<string, string>, id: string): string {
  return cls[id] ?? (id.startsWith("tag:") ? PHONE_HIDE : "");
}

/** Visible-column ids per view, persisted in localStorage; toggle flips one id.
 *
 *  The key is VERSIONED because the ids are (`mlo-cols4-*`): a list written by
 *  an older build holds ids this one no longer has, and the reader that only
 *  kept the ids it recognised turned that into a half-empty table — the owner's
 *  Albums view drew its expand chevron and nothing else, the Tracks view its
 *  row number, and no column chooser entry looked wrong, because the columns
 *  the old build never offered were never unticked. So a list from the previous
 *  key is MIGRATED, not trusted: the ids it still has are kept (a deliberate
 *  choice survives), and every column this build ships visible by default is
 *  added, because a prefs entry cannot be evidence about a column that did not
 *  exist when it was written. Unticking anything after that is stored under the
 *  new key and honoured for good.
 *
 *  A list under the CURRENT key is not evidence either, and the reason is
 *  sharper than the key's own: a list of three ids looks exactly like a reader
 *  who once unticked four columns. The owner's album tracklist read three
 *  columns (`["num","cover","title"]` of seven) behind a Columns menu that
 *  listed all seven ticked — the menu draws the list this hook RETURNS — so
 *  nothing on screen said where the other four had gone, and obeying the list
 *  kept it that way forever.
 *
 *  What is therefore stored is the reader's CHOICE, as a versioned record
 *  beside the list (`mlo-coldft-*`): `{v: 2, removed, added}` — which of the
 *  columns this build SHIPS visible they took away, and which columns it does
 *  not ship (the `defHidden` built-ins, and their own tag columns) they put
 *  there. The drawn set is DERIVED from it — every shipped column minus
 *  `removed`, plus `added`, in the table's own order — so a column a later
 *  build starts shipping appears by itself, a column this build no longer has
 *  is simply not there, and no stored list can quietly subtract a column the
 *  reader never touched. A record WITHOUT that version (a v1 fingerprint, a
 *  hand-edited key, another shape) is UNKNOWN rather than a choice: it is the
 *  same MIGRATION the v3 key gets — the ids the stored list still has are
 *  kept, every column this view draws by default comes back, and the result
 *  is re-recorded as v2, once. An untick after that is a `removed` entry and
 *  sticks across reloads for good; showing a column again takes it out of
 *  `removed`. */
export function useColumnPrefs(key: string, defs: Col[]): [string[], (id: string) => void] {
  // v4: ids are versioned (the key moves when they change), and a v3 list is
  // migrated rather than filtered — see the block comment above.
  const storageKey = `mlo-cols4-${key}`;
  const legacyKey = `mlo-cols3-${key}`;
  const recordKey = `mlo-coldft-${key}`;
  const allVisible = defs.filter((c) => !c.defHidden).map((c) => c.id);
  /** The columns THIS build ships visible by default: the built-ins. The
   *  `defHidden` built-ins and the reader's own tag columns are not shipped —
   *  they are what the record's `added` is for. */
  const shipped = defs.filter((c) => !c.defHidden && !c.tag).map((c) => c.id);
  const ids = new Set(defs.map((d) => d.id));
  const shippedIds = new Set(shipped);
  // A list that would leave the table nothing but furniture (the row number,
  // the cover) is not a choice anyone made about which columns to READ: the
  // owner's Tracks view drew a `#` header and a column of row numbers with
  // nothing in it (their words: "NOTHING SHOWS UP"), and the Columns menu
  // looked right, because the columns this build never drew were never offered
  // to be unticked. The view's own defaults are used instead — the same rule as
  // the v3 note above, one step further on.
  const chromeIds = new Set(defs.filter((d) => d.chrome).map((d) => d.id));
  const drawsData = (list: string[]) => list.some((id) => !chromeIds.has(id));
  /** The reader's choice as a record: what they took OUT of the shipped set
   *  and what they ADDED to it. Deriving both from a drawn list is what makes
   *  one toggle and one repair the same operation (see `remember`). */
  const asChoice = (list: string[]): ColRecord => ({
    v: 2,
    removed: shipped.filter((id) => !list.includes(id)),
    added: [...new Set(list)].filter((id) => ids.has(id) && !shippedIds.has(id)),
  });
  /** The columns a record draws, in the table's own order: every shipped
   *  column the reader did not remove, plus everything they added. This is the
   *  ONLY place a drawn set comes from — a stored list is never read as one. */
  const drawnBy = (record: ColRecord) =>
    defs.filter((c) => !record.removed.includes(c.id) && (shippedIds.has(c.id) || record.added.includes(c.id)))
      .map((c) => c.id);
  /** Store a choice: the record is the state, and the list beside it is the
   *  menu's copy of what that state draws — written together, so the two
   *  cannot disagree. */
  const remember = (record: ColRecord) => {
    try {
      localStorage.setItem(recordKey, JSON.stringify(record));
      localStorage.setItem(storageKey, JSON.stringify(drawnBy(record)));
    } catch {
      /* ignore */
    }
  };
  /** The record as v2, or null for anything else — no record at all, a
   *  hand-edited key, and every record from before the version. A v1 record is
   *  a fingerprint of a list rather than a statement about the reader's choice,
   *  so it is UNKNOWN, not obeyed. */
  const readRecord = (): ColRecord | null => {
    try {
      const raw = localStorage.getItem(recordKey);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Partial<ColRecord> | null;
      if (!parsed || typeof parsed !== "object" || parsed.v !== 2) return null;
      const listOf = (x: unknown) => (Array.isArray(x) ? x.filter((id): id is string => typeof id === "string") : []);
      return { v: 2, removed: listOf(parsed.removed), added: listOf(parsed.added) };
    } catch {
      return null;
    }
  };
  const [visible, setVisible] = useState<string[]>(() => {
    try {
      const record = readRecord();
      if (record) {
        const next = drawnBy(record);
        if (next.length && drawsData(next)) {
          // A shipped column this record never mentioned is drawn by it, so the
          // menu's copy is put back in step after a build changed its defaults.
          if (localStorage.getItem(storageKey) !== JSON.stringify(next)) {
            localStorage.setItem(storageKey, JSON.stringify(next));
          }
          return next;
        }
        // A record that hides every data column is the same furniture-only
        // state a list could be — the defaults are drawn and re-recorded.
        remember(asChoice(allVisible));
        return allVisible;
      }
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const arr = JSON.parse(raw) as string[];
        const kept = arr.filter((x) => ids.has(x));
        // No v2 record: the list is a reading of the build that WROTE it, so
        // the ids it still has are kept and every column this view draws by
        // default comes back — then the result is recorded as v2, once.
        const base = kept.length && drawsData(kept) ? kept : allVisible;
        const next = [...new Set([...base, ...allVisible])];
        remember(asChoice(next));
        return next;
      }
      const old = localStorage.getItem(legacyKey);
      if (old) {
        const arr = JSON.parse(old) as string[];
        const kept = arr.filter((x) => ids.has(x));
        const next = [...new Set([...kept, ...allVisible])];
        remember(asChoice(next));
        localStorage.removeItem(legacyKey);
        return next;
      }
    } catch {
      /* fall through to defaults */
    }
    return allVisible;
  });
  const toggle = (id: string) =>
    setVisible((v) => {
      const next = v.includes(id) ? v.filter((x) => x !== id) : [...v, id];
      remember(asChoice(next));
      return next;
    });
  return [visible, toggle];
}

/** User-added tag columns per view, persisted in localStorage. The id
 *  derives from the tag (`tag:MOOD`), so adding the same tag twice is a
 *  no-op and the column survives a reload. `add` returns the new id ("" when
 *  the tag is empty or already has a column) so callers can show it right
 *  away — a column the user just created should not start hidden. */
export function useCustomColumns(key: string): [CustomCol[], (tag: string, label?: string) => string, (id: string) => void] {
  const storageKey = `mlo-customcols-${key}`;
  const [customs, setCustoms] = useState<CustomCol[]>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw) as CustomCol[];
        if (Array.isArray(parsed))
          return parsed.filter((c) => c && typeof c.id === "string" && typeof c.tag === "string" && !!c.tag.trim());
      }
    } catch {
      /* fall through to no custom columns */
    }
    return [];
  });
  const save = (next: CustomCol[]) => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  };
  const add = (tag: string, label?: string) => {
    const t = tag.trim().replace(/^tag:/i, "").toUpperCase();
    if (!t) return "";
    const id = `tag:${t}`;
    if (customs.some((c) => c.id === id)) return "";
    const next = [...customs, { id, label: (label ?? "").trim() || t, tag: t }];
    setCustoms(next);
    save(next);
    return id;
  };
  const remove = (id: string) => {
    const next = customs.filter((c) => c.id !== id);
    setCustoms(next);
    save(next);
  };
  return [customs, add, remove];
}

/** Cell text of a tag column: the row's tags first (case-insensitive key),
 *  then its technical record, else "". Album rows pass their `meta` as
 *  `tags` — it is the same tag map, one level up. `tag` is either the bare
 *  tag or the column id (`tag:MOOD`) — callers hold one or the other. */
export function customColValue(row: { tags?: unknown; tech?: unknown } | null | undefined, tag: string): string {
  const want = tag.replace(/^tag:/i, "").toLowerCase();
  for (const rec of [row?.tags, row?.tech]) {
    if (!rec || typeof rec !== "object") continue;
    const key = Object.keys(rec).find((k) => k.toLowerCase() === want);
    const v = key ? (rec as Record<string, unknown>)[key] : null;
    if (v !== null && v !== undefined && v !== "") return String(v);
  }
  return "";
}

/** Custom columns as table columns — `scope` picks the dotted sort key
 *  rowValue resolves: "tags.X" on a track table, "meta.X" on an album one. */
export function customCols(customs: CustomCol[], scope: "tags" | "meta"): Col[] {
  return customs.map((c) => ({ id: c.id, label: c.label, sortKey: `${scope}.${c.tag}`, tag: c.tag }));
}

/** Drag-resized column widths per table view, persisted in localStorage.
 * Absent entries fall back to the fluid % classes; double-clicking a
 * handle (or the Columns menu reset) clears them.
 *
 * A stored width is a READING PREFERENCE, never evidence about this build —
 * the same rule `useColumnPrefs` applies to a stored visible-id list. It is
 * applied to a column's `<th>` as an inline width in a FIXED-layout table, so a
 * value that is not a sane number does not merely look wrong, it takes the
 * column out of the view: a stored `0` (or a hand-edited/imported string, or a
 * JSON shape from an older key) pinned every data column to 0 px, which is an
 * Albums view of artist headers over thin chevron rows, a Tracks view showing
 * nothing but the row numbers, and no cover images — while the columns menu
 * still listed every column, because it reads the VISIBLE list. Anything the
 * drag handle itself could not have produced (the setter clamps to 40-900) is
 * dropped on read, so the column keeps its own floor from the `_COL_W` maps. */
function sanitizeWidths(raw: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
    const n = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(n) || n < 40 || n > 900) continue;
    out[id] = Math.round(n);
  }
  return out;
}

/** The stored widths, sanitized (see sanitizeWidths). */
export function useColumnWidths(key: string): [Record<string, number>, (id: string, px: number) => void, () => void] {
  const storageKey = `mlo-colw-${key}`;
  const [widths, setWidths] = useState<Record<string, number>>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) return sanitizeWidths(JSON.parse(raw));
    } catch {
      /* ignore */
    }
    return {};
  });
  const set = (id: string, px: number) =>
    setWidths((w) => {
      const next = { ...w, [id]: Math.max(40, Math.min(900, Math.round(px))) };
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  const reset = () => {
    setWidths({});
    try {
      localStorage.removeItem(storageKey);
    } catch {
      /* ignore */
    }
  };
  return [widths, set, reset];
}

/** The stored widths as the table can actually SHOW them, plus the ref the
 *  table's `overflow-x-auto` wrapper takes.
 *
 *  The rule — the columns auto-fit the screen, and a stored width may only eat
 *  space that exists: a fixed layout makes the sum of its columns' own widths
 *  the table's floor (CSS 2.1 §17.5.2.1: the used width is the greater of the
 *  table's width and that sum), so a stored map the drag handle itself could
 *  have produced — every value inside its own 40-900 clamp — can draw a table
 *  wider than the screen and push every column after the widened one off the
 *  right edge. Measured: the owner's Artists view with four stored 300 px
 *  columns drew 1420 px in a 1200 px box although the columns' own floors sum
 *  to 596, and `mlo-colw-album-tracks` = {title:900, genre:900, bitrate:900,
 *  dur:900} drew 3848 px of album tracklist in the same 1200 px box.
 *
 *  So the widths a reader stored are a PREFERENCE about how the width is
 *  SPENT, never a floor the table has to be that wide. The floors come first
 *  (each column keeps at least its own `_COL_W` width), then what is left over
 *  is shared among the columns the reader sized, in proportion to how much
 *  MORE than its floor each one asked for:
 *
 *      shown(i) = floor(i) + (stored(i) - floor(i)) * room / Σ excess
 *
 *  which leaves the table exactly as wide as it would be with no stored widths
 *  at all: `w-full` when the floors fit, and the floors' own sum when they do
 *  not (the sideways scroll R313 keeps for a table whose floors genuinely
 *  cannot fit — never a preference value).
 *
 *  Nothing here needs a second copy of the floors: `drawn` is the columns the
 *  table draws, in the order it draws them (`defs.filter(visible).map(id)`), so
 *  each stored id pairs with its own header cell, and every reading is taken
 *  from the table itself inside one layout pass — the stored widths switched
 *  off and the table asked for the width its own columns need, which is the
 *  floor TABLE_FIT asks it for (`md:w-max`) with the reader's widths removed.
 *  The floor's other half, `min-width: 100%`, comes off for that reading: left
 *  on, it pins the table to the box instead of to its columns and the fit
 *  would collapse every stored width to the box. Below `md` the floor is not
 *  in play at all (`w-full`, the fold's own columns) so the reading stays the
 *  zero-width one of old — the phone's own layout, measured the same way.
 *  Nothing paints in between. */
export function useFittedWidths(
  widths: Record<string, number>,
  drawn: string[]
): [Record<string, number>, (el: HTMLDivElement | null) => void] {
  const box = useRef<HTMLDivElement | null>(null);
  // Whether the wrapper is MOUNTED — the tables live behind a view switch, so
  // the effect's first run has the ref empty and its dependencies unchanged
  // when the reader opens the view. A callback ref says when to look again.
  const [mounted, setMounted] = useState(false);
  const [fitted, setFitted] = useState<Record<string, number> | null>(null);
  // The drawn ids as one string: an array prop is a new object every render.
  const drawnKey = drawn.join(",");
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const read = () => {
      const table = el.querySelector("table");
      if (!table) return;
      const ths = [...table.querySelectorAll<HTMLElement>("thead th")];
      const shown = ths.map((th) => th.style.width);
      // The columns the reader sized, in the order the table draws them.
      const sized = drawnKey ? drawnKey.split(",").filter((id) => (widths[id] ?? 0) > 0) : [];
      const drawnThs = ths.filter((th) => th.style.width !== "");
      if (sized.length !== drawnThs.length || sized.length === 0) {
        // Nothing to fit, or a header the widths do not account for (a column
        // drawn by something else): leave the map exactly as stored.
        if (fitted) setFitted(null);
        return;
      }
      // The floors: the stored widths off, the table asked for the width its
      // own columns need (the floor TABLE_FIT asks for at `md` and up; below
      // `md` the fold's own columns are all there is, so the reading keeps the
      // old pinned-to-zero one). `min-width: 100%` is the floor's fill half
      // and would pin the table to the box instead — see the notes above.
      const md = window.matchMedia("(min-width: 768px)").matches;
      ths.forEach((th) => { th.style.width = ""; });
      const own = table.style.width;
      const ownMin = table.style.minWidth;
      table.style.minWidth = "0px";
      table.style.width = md ? "max-content" : "0px";
      const floors = ths.map((th) => Math.round(th.getBoundingClientRect().width));
      const floorsW = table.scrollWidth;
      // The table the reader asked for, at full size, and what it wants above
      // the floors — measured per column, not summed, because a column cannot
      // be shown narrower than its own floor.
      const asked = sized.map((id, i) => {
        const floor = floors[ths.indexOf(drawnThs[i])] ?? 0;
        return { id, floor, excess: Math.max(0, widths[id] - floor) };
      });
      table.style.width = own;
      table.style.minWidth = ownMin;
      ths.forEach((th, i) => { th.style.width = shown[i]; });
      const excess = asked.reduce((n, c) => n + c.excess, 0);
      const room = Math.max(0, el.clientWidth - floorsW);
      const scale = excess > room && excess > 0 ? room / excess : 1;
      const next: Record<string, number> = {};
      for (const c of asked) next[c.id] = c.floor + Math.round(c.excess * scale);
      // Only when something actually changed: the ResizeObserver fires on every
      // step of a resize, and a new object identity would re-render the page's
      // whole table each time.
      const before = Object.entries(fitted ?? {}).map(([id, w]) => `${id}:${w}`).join(",");
      const after = Object.entries(next).map(([id, w]) => `${id}:${w}`).join(",");
      if (before !== after) setFitted(scale === 1 ? null : next);
    };
    read();
    // The box changes without this hook re-rendering: a window resize, the
    // sidebar folding away, a phone turning.
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [widths, drawnKey, mounted]);

  // Stable identity: a ref callback that changes every render makes React call
  // it with null and the element again on every render.
  const attach = useCallback((el: HTMLDivElement | null) => {
    box.current = el;
    setMounted(!!el);
  }, []);
  return [fitted ? { ...widths, ...fitted } : widths, attach];
}

/** Per-view "which columns are visible" menu with a width reset. An optional
 * second section (extraCols) covers a nested table that lives inside the same
 * view — e.g. the tracklist under an expanded album row. `iconOnly` renders
 * a small square icon button for placement inside a table's corner header
 * cell (where a labeled button would shout). With `onAddCustom` the menu also
 * grows the tag-column form; columns carrying a `tag` get a remove control. */
export function ColumnsMenu({
  cols,
  visible,
  onToggle,
  fullDates,
  onFullDates,
  onResetWidths,
  hasCustomWidths,
  title = "Visible columns",
  extraCols,
  extraVisible,
  onExtraToggle,
  extraOnRemoveCustom,
  extraTitle = "Tracklist columns",
  iconOnly = false,
  onAddCustom,
  onRemoveCustom,
}: {
  cols: Col[];
  visible: string[];
  onToggle: (id: string) => void;
  fullDates?: boolean;
  onFullDates?: (v: boolean) => void;
  onResetWidths?: () => void;
  hasCustomWidths?: boolean;
  title?: string;
  extraCols?: Col[];
  extraVisible?: string[];
  onExtraToggle?: (id: string) => void;
  /** Removes a tag column from the nested table's list — the same list the
   *  primary table's `onRemoveCustom` edits when the two share one customs
   *  key (the album page and the library's expanded album rows do). */
  extraOnRemoveCustom?: (id: string) => void;
  extraTitle?: string;
  iconOnly?: boolean;
  /** Adds a tag-backed column (see useCustomColumns) — the menu shows the
   *  "Add tag column" form only when this is wired. */
  onAddCustom?: (tag: string, label?: string) => void;
  onRemoveCustom?: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [tag, setTag] = useState("");
  const [label, setLabel] = useState("");
  const add = () => {
    if (!tag.trim()) return;
    onAddCustom?.(tag, label);
    setTag("");
    setLabel("");
  };
  return (
    <div className="relative">
      <button
        className={
          iconOnly
            ? `p-1.5 rounded-lg border transition-colors tap min-w-11 md:min-w-0 ${
                open
                  ? "text-accent border-accent/50 bg-raise"
                  : "border-border bg-panel/60 text-zinc-500 hover:text-white hover:bg-raise"
              }`
            : `btn-ghost !py-1.5 text-xs tap ${open ? "!text-white !bg-raise" : ""}`
        }
        onClick={() => setOpen(!open)}
        title="Columns"
        aria-label="Columns"
      >
        <Columns3 className="h-3.5 w-3.5" />
        {!iconOnly && " Columns"}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className={`absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-lg p-2 ${onAddCustom ? "w-56" : "w-48"} shadow-2xl`}>
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">{title}</div>
            {cols.map((c) => (
              <div key={c.id} className="flex items-center gap-2 px-2 py-1.5 text-xs hover:bg-panel rounded tap">
                <label className="flex items-center gap-2 cursor-pointer min-w-0 flex-1">
                  <input type="checkbox" checked={visible.includes(c.id)} onChange={() => onToggle(c.id)} className="" />
                  <span className="truncate" title={c.tag ? `Tag: ${c.tag}` : undefined}>{c.label || "Cover"}</span>
                </label>
                {c.tag && onRemoveCustom && (
                  <button
                    className="shrink-0 text-zinc-600 hover:text-red-400"
                    title={`Remove the ${c.label} column`}
                    aria-label={`Remove the ${c.label} column`}
                    onClick={() => onRemoveCustom(c.id)}
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            ))}
            {onAddCustom && (
              <>
                <div className="border-t border-border my-1.5" />
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">Add tag column</div>
                <div className="flex gap-1 px-2">
                  <input
                    className="input !py-1 !px-2 text-xs min-w-0 flex-1 tap"
                    placeholder="TAG"
                    title="Tag name — e.g. MOOD, COMPOSER, CATALOGNUMBER"
                    value={tag}
                    onChange={(e) => setTag(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && add()}
                  />
                  <input
                    className="input !py-1 !px-2 text-xs min-w-0 flex-1 tap"
                    placeholder="Label"
                    title="Optional header label — the tag name is used when empty"
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && add()}
                  />
                </div>
                <button className="btn-ghost w-full !py-1 text-xs mt-1 tap" onClick={add} disabled={!tag.trim()}>
                  Add column
                </button>
              </>
            )}
            {extraCols && onExtraToggle && extraVisible && (
              <>
                <div className="border-t border-border my-1.5" />
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">{extraTitle}</div>
                {extraCols.map((c) => (
                  <div key={c.id} className="flex items-center gap-2 px-2 py-1.5 text-xs hover:bg-panel rounded tap">
                    <label className="flex items-center gap-2 cursor-pointer min-w-0 flex-1">
                      <input type="checkbox" checked={extraVisible.includes(c.id)} onChange={() => onExtraToggle(c.id)} className="" />
                      <span className="truncate" title={c.tag ? `Tag: ${c.tag}` : undefined}>{c.label || "Cover"}</span>
                    </label>
                    {c.tag && extraOnRemoveCustom && (
                      <button
                        className="shrink-0 text-zinc-600 hover:text-red-400"
                        title={`Remove the ${c.label} column`}
                        aria-label={`Remove the ${c.label} column`}
                        onClick={() => extraOnRemoveCustom(c.id)}
                      >
                        <X className="h-3 w-3" />
                      </button>
                    )}
                  </div>
                ))}
              </>
            )}
            {onFullDates && (
              <>
                <div className="border-t border-border my-1.5" />
                <label className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer hover:bg-panel rounded">
                  <input type="checkbox" checked={!!fullDates} onChange={(e) => onFullDates(e.target.checked)} />
                  Show full dates
                </label>
              </>
            )}
            {onResetWidths && (
              <>
                <div className="border-t border-border my-1.5" />
                <button
                  className="w-full text-left px-2 py-1.5 text-xs text-zinc-400 hover:text-white hover:bg-panel rounded disabled:opacity-40"
                  onClick={() => {
                    onResetWidths();
                    setOpen(false);
                  }}
                  disabled={!hasCustomWidths}
                  title="Restore the default fluid column widths"
                >
                  Reset column widths
                </button>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/** Drag handle that resizes the column it lives in (persisted per view by
 * the caller). Lives at a th's right edge; the th needs `relative`. */
export function ColumnResizer({ width, onDrag, onReset }: {
  width: number | undefined;
  onDrag: (px: number) => void;
  onReset: () => void;
}) {
  const cleanupRef = useRef<(() => void) | null>(null);
  // A drag that's in progress when this th unmounts (sort/filter change,
  // navigation) would otherwise leak both window listeners and the
  // col-resize cursor.
  useEffect(() => () => cleanupRef.current?.(), []);
  const start = (e: ReactMouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const x0 = e.clientX;
    const w0 = width ?? (e.currentTarget.parentElement as HTMLElement)?.getBoundingClientRect().width ?? 100;
    const move = (ev: MouseEvent) => onDrag(ev.clientX - x0 + w0);
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      cleanupRef.current = null;
    };
    cleanupRef.current = up;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  return (
    <span
      className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize hover:bg-accent/40"
      onMouseDown={start}
      onDoubleClick={(e) => {
        e.stopPropagation();
        onReset();
      }}
      title="Drag to resize · double-click to reset"
      onClick={(e) => e.stopPropagation()}
    />
  );
}
