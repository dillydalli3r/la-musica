/* The ONE disc vocabulary, mirrored for the browser.
 *
 * The Python original is `mlo/discs.py` — the single source of truth the
 * library's search, mover and layout scan all ask "is this folder a disc of
 * its parent?". The import wizard's album detection used to carry its OWN,
 * narrower spelling (`/^(cd|disc|disk)\s*\d+$/i`), so a release whose disc
 * folders were named "CD_1", "Disc 2 (FLAC)", "Volume 1", "BD1" or the bare
 * "1"/"2" split into an album per disc — the very folders the LIBRARY folds
 * into one album. This module is the wizard side of that one vocabulary; the
 * rules below are a line-for-line mirror of `mlo/discs.py`, kept honest by
 * `tools/test_disc_vocab.py` (which runs `tools/disc_vocab_probe.mjs` against
 * the Python functions and asserts they agree name for name).
 *
 * Pure TypeScript: no imports of other app modules, no JSX, so plain Node 24
 * can import the .ts file directly (Node strips the type annotations) without
 * a build step. Add a spelling HERE, in `mlo/discs.py`, never in a wizard-local
 * regex again. */

/* The decoration a share wraps its folder names in: "CD1 [FLAC]" -> "CD1",
 * "Album (1994)" -> "Album". `mlo.discs.DECOR_RE`. */
const DECOR_RE = /[\s._-]*[(\[]([^)\]]*)[)\]][\s._-]*$/;

/* A disc WORD plus its index: "CD1", "CD 1", "CD-1", "CD_1", "Disc 2",
 * "Disk1", "Volume 1", "DVD 2", "BD1" — the spelling a rip's own folder uses.
 * `mlo.discs.DISC_DIR_WORD_RE`, anchored here because Python calls it with
 * `fullmatch`; the capture is the index Python re-searches for. */
const DISC_DIR_WORD_FULL = /^(?:cd|disc|disk|dvd|bd|volume|vol)[\s._-]?(\d{1,2})$/i;

/* ...and the bare index a plain rip numbers its folders with: "1", "02".
 * `mlo.discs.DISC_DIR_INDEX_RE` (fullmatch). */
const DISC_DIR_INDEX_FULL = /^\d{1,2}$/;

/* The loose token a FILE (or folder) name can state a disc with, anywhere in
 * the name: "CD1", "Disc 2 [FLAC]", "Album CD2". `mlo.discs.DISC_TOKEN_RE`. */
const DISC_TOKEN_RE =
  /(?:^|[^A-Za-z0-9])(?:cd|disc|disk|dvd|bd|volume|vol)\s*[-_.]?\s*(\d{1,2})(?![0-9])/i;

/* "1-03 Title": the library's own D-TT convention — the first number names the
 * disc, and only when a track number follows it ("12 - Title" is a track, not
 * disc 12). `mlo.discs.DISC_LEAD_RE`. */
const DISC_LEAD_RE = /^(\d{1,2})\s*-\s*\d{1,3}(?:\D|$)/;

/* "(2) 01 - Title": the disc in brackets in front of the track number.
 * `mlo.discs.DISC_BRACKET_RE`. */
const DISC_BRACKET_RE = /^[(\[]\s*(\d{1,2})\s*[)\]]\s*/;

function str(name: string): string {
  return name == null ? "" : String(name);
}

/* `os.path.basename` over a name whose separators are the forward slash
 * (`mlo/discs.py` normalises backslashes first, so the two engines read a
 * Windows and a POSIX path the same). */
function baseName(p: string): string {
  const s = p.replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  return i < 0 ? s : s.slice(i + 1);
}

/* `os.path.dirname` over forward-slash separators. */
function dirName(p: string): string {
  const s = p.replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  if (i < 0) return "";
  let head = s.slice(0, i + 1);
  if (head && head !== "/".repeat(head.length)) head = head.replace(/\/+$/, "");
  return head;
}

/* `os.path.splitext`, whose leading-dot rule ("*.flac" keeps its name) the
 * mirror has to keep: the stem feeds the disc-token read. */
function splitExt(p: string): [string, string] {
  const sepIndex = p.lastIndexOf("/");
  const dotIndex = p.lastIndexOf(".");
  if (dotIndex > sepIndex) {
    let filenameIndex = sepIndex + 1;
    while (filenameIndex < dotIndex) {
      if (p[filenameIndex] !== ".") {
        return [p.slice(0, dotIndex), p.slice(dotIndex)];
      }
      filenameIndex += 1;
    }
  }
  return [p, ""];
}

/* A disc number is 1 or more ("CD0" states nothing). `mlo.discs._positive`. */
function positive(value: number): number | null {
  return value >= 1 ? value : null;
}

/** "CD1 [FLAC]" -> "CD1"; "Album (1994)" -> "Album".
 *  Mirror of `mlo.discs.strip_decoration`. */
export function stripDecoration(name: string): string {
  let s = str(name);
  let m = DECOR_RE.exec(s);
  while (m) {
    s = s.slice(0, m.index);
    m = DECOR_RE.exec(s);
  }
  return s.trim();
}

/** Disc number a folder or FILE name claims ("CD1", "Disc 2 [FLAC]",
 *  "1-03 rip", "(2) 01 x"), or null when the name says nothing.
 *
 *  The loose reading, for names that carry the disc as a token: a file name
 *  (".../CD2/01 - x.flac", "1-03 x.flac") and a folder that states a disc in
 *  words. A BARE index is deliberately not a disc here — a folder called "25"
 *  may be the album — that spelling counts only with its siblings' evidence
 *  (`isDiscParent`, `discNumberPathOf`). Mirror of `mlo.discs.disc_number_of_name`,
 *  kept module-private: the contract exports only the five names below. */
function discNumberOfName(name: string): number | null {
  const base = baseName(name.replace(/\\/g, "/")).trim();
  const stem = stripDecoration(splitExt(base)[0] || base);
  const tok = DISC_TOKEN_RE.exec(stem);
  if (tok) return positive(Number(tok[1]));
  const lead = DISC_LEAD_RE.exec(stem);
  if (lead) return positive(Number(lead[1]));
  const bracket = DISC_BRACKET_RE.exec(stem);
  return bracket ? positive(Number(bracket[1])) : null;
}

/** The disc a name states when the WHOLE name is its index — "1", "02" — else
 *  null. Mirror of `mlo.discs.bare_disc_index`.
 *
 *  A bare index is weak evidence on its own: a rip that numbers its folders
 *  "1"/"2" states its discs that way, and so does a folder holding the album
 *  "25". It is read as a disc only where the sibling folders settle it
 *  (`isDiscParent`, `discNumberPathOf`). */
export function bareDiscIndex(name: string): number | null {
  const base = baseName(str(name).replace(/\\/g, "/")).trim();
  if (!DISC_DIR_INDEX_FULL.test(base)) return null;
  return positive(Number(base));
}

/** The disc a DIRECTORY name states, or null when the folder is not a disc
 *  folder of its parent. Mirror of `mlo.discs.disc_dir_number`.
 *
 *  The ONE rule the wizard's album detection, the search's candidate tree, the
 *  importer's mover and the readiness check ask, for a folder that names its
 *  disc: "CD1", "CD 1", "CD-1", "Disk1", "Disc 2", "Volume 1", "DVD 2",
 *  "CD1 [FLAC]" (the share's decoration is stripped), plus the loose spellings
 *  `discNumberOfName` reads. Anything else — "Album [FLAC]", "Extras",
 *  "Log+Cue", "Scans" — is not a disc folder whatever else it may be. The bare
 *  index ("1", "02") is answered by `bareDiscIndex`, which needs the siblings
 *  to count. */
export function discDirNumber(name: string): number | null {
  const base = baseName(str(name).replace(/\\/g, "/")).trim();
  const bare = stripDecoration(base);
  if (!bare) return null;
  const word = DISC_DIR_WORD_FULL.exec(bare);
  if (word) return positive(Number(word[1]));
  if (DISC_DIR_INDEX_FULL.test(bare)) return null; // bare index: see bareDiscIndex
  return discNumberOfName(bare);
}

/** Whether a folder's album-bearing CHILD folders make it the parent of a
 *  multi-disc release — ONE album, its discs below it — or not. Mirror of
 *  `mlo.discs.is_disc_parent`.
 *
 *  `childNames` are the names of the children that carry album material
 *  (.log/.cue/audio), as the caller has already established:
 *
 *    * a child that NAMES a disc — "CD1", "Disc 2", "Volume 1", "1-03 rip",
 *      "CD1 [FLAC]" — makes the parent the album, and ANY other album-bearing
 *      child rides along instead of splitting off as an album of its own (a
 *      stray log/cue folder, an artwork or bonus folder);
 *    * a child that is only a BARE index ("1", "02") counts when the children
 *      are a numbered set — "1"+"2" is a rip's disc split, while a lone "25"
 *      is the album "25" and nothing else;
 *    * no child names a disc -> not a disc parent: the children are albums of
 *      their own. */
export function isDiscParent(childNames: string[]): boolean {
  const names = (childNames ?? []).map((n) => str(n));
  if (names.some((n) => discDirNumber(n) !== null)) return true;
  const numbers = names
    .map((c) => bareDiscIndex(c))
    .filter((n): n is number => n !== null)
    .sort((a, b) => a - b);
  // a numbered SET, not two unrelated folders that happen to be numbers: the
  // run must start at 1 and have no holes ("1","2","3" is a rip, "25","30" is
  // two albums).
  return numbers.length >= 2 && numbers.every((n, i) => n === i + 1);
}

/** The disc a FILE path belongs to, or null when nothing states one. Mirror of
 *  `mlo.discs.disc_number_of_path`, minus the directory listing.
 *
 *  The file's own name first, then the folder it sits in — including a folder
 *  that is only a bare index. Python reads the bare-index folder's siblings
 *  (a numbered set) before trusting it; the browser has no listing here, so
 *  the bare-index parent answers from the path alone — the shape the wizard's
 *  detection needs for the "…/1/01 - x.flac" + "…/2/01 - y.flac" split it is
 *  itself walking. An ordinary "…/CD2/01 - x.flac" or "…/Album/01 - x.flac"
 *  shape never reaches the bare-index branch. */
export function discNumberPathOf(path: string): number | null {
  const p = str(path).replace(/\\/g, "/");
  const own = discNumberOfName(baseName(p));
  if (own !== null) return own;
  const parent = dirName(p);
  const stated = discDirNumber(baseName(parent));
  if (stated !== null) return stated;
  const bare = bareDiscIndex(baseName(parent));
  if (bare !== null) return bare;
  return null;
}