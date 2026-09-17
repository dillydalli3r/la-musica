// Client-side mirror of mlo.lyrics._syllabify_token / _elrc_split_words
// (mlo/lyrics.py). The lyric editor's tap-along syllable chips must split
// words the same way the deterministic backend builder would, so
// hand-stamped syllable ELRC matches the backend output byte for
// byte. Keep the two in sync.

const CJK_RE = /[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]/;
const VOWELS = new Set("aeiouyàáâãäåæèéêëìíîïòóôõöøùúûüýÿ");
const DIGRAPHS = new Set(["ch", "sh", "th", "ph", "wh", "ck", "ng", "gh", "qu"]);
const WORD_CHAR = /[\p{L}\p{N}_]/u;

/** The backend's word tokenizer (mlo.lyrics._elrc_split_words): whitespace
 * words for Latin text, but a CJK-bearing piece sweeps per character while
 * any Latin/digit run inside it stays glued — "カラオケKEIKO" tokenizes to
 * カ ラ オ ケ KEIKO rather than one piece per codepoint. */
export function splitElrcWords(line: string): string[] {
  const out: string[] = [];
  for (const piece of line.trim().split(/\s+/)) {
    if (!piece) continue;
    if (!CJK_RE.test(piece)) {
      out.push(piece);
      continue;
    }
    let buf = "";
    for (const ch of piece) {
      if (CJK_RE.test(ch)) {
        if (buf) {
          out.push(buf);
          buf = "";
        }
        out.push(ch);
      } else {
        buf += ch;
      }
    }
    if (buf) out.push(buf);
  }
  return out.length ? out : [line];
}

/** Split one word token into syllables. CJK: one character (kana = one
 * mora) per syllable, with glued Latin/digit runs then split by the Latin
 * rules (the backend runs _syllabify_token over each _elrc_split_words
 * token, so "カラオケKEIKO" → カ ラ オ ケ KEI KO). Latin: maximal vowel runs
 * are nuclei; the cluster between two nuclei splits before a single
 * consonant, between a pair, before a digraph, or after the first
 * consonant of a longer cluster; "y" is a vowel except word-initially (see
 * the Python twin for the exact rules). Returns [word] when no split is
 * found. */
export function syllabifyToken(word: string): string[] {
  if (!word) return [word];
  if (CJK_RE.test(word)) {
    return splitElrcWords(word).flatMap((w) => (CJK_RE.test(w) ? [w] : syllabifyToken(w)));
  }
  const first = word.search(WORD_CHAR);
  if (first === -1) return [word];
  let last = word.length - 1;
  while (last >= 0 && !WORD_CHAR.test(word[last])) last--;
  const lead = word.slice(0, first);
  const core = word.slice(first, last + 1);
  const trail = word.slice(last + 1);
  if (Array.from(core).length <= 3) return [word];
  const low = core.toLowerCase();
  const isV = (ch: string, i: number) => VOWELS.has(ch) && !(ch === "y" && i === 0);
  const runs: [number, number][] = [];
  let start: number | null = null;
  for (let i = 0; i < low.length; i++) {
    if (isV(low[i], i)) {
      if (start === null) start = i;
    } else if (start !== null) {
      runs.push([start, i]);
      start = null;
    }
  }
  if (start !== null) runs.push([start, low.length]);
  // a mid-run y starts (prev char is a vowel: be-yond, ka-yak) or closes
  // (prev char is a consonant: try-ing) its own nucleus
  const splitRuns: [number, number][] = [];
  for (const [a, b] of runs) {
    let a0 = a;
    if (low[a0] === "y" && b - a0 > 1 && VOWELS.has(low[a0 + 1])) {
      splitRuns.push([a0, a0 + 1]);
      a0 += 1;
    }
    let prevVowel = false;
    for (let i = a0; i < b; i++) {
      if (low[i] === "y" && i > a0 && i < b - 1) {
        if (prevVowel) {
          splitRuns.push([a0, i]);
          a0 = i;
        } else {
          splitRuns.push([a0, i + 1]);
          a0 = i + 1;
        }
      }
      prevVowel = VOWELS.has(low[i]);
    }
    splitRuns.push([a0, b]);
  }
  if (splitRuns.length <= 1) return [word];
  const cuts: number[] = [];
  for (let k = 0; k < splitRuns.length - 1; k++) {
    const cStart = splitRuns[k][1];
    const cEnd = splitRuns[k + 1][0];
    const n = cEnd - cStart;
    if (n <= 1) cuts.push(cStart);
    else if (n === 2) cuts.push(DIGRAPHS.has(low.slice(cStart, cEnd)) ? cStart : cStart + 1);
    else cuts.push(cStart + 1);
  }
  const syls: string[] = [];
  let prev = 0;
  for (const cut of cuts) {
    syls.push(core.slice(prev, cut));
    prev = cut;
  }
  syls.push(core.slice(prev));
  const parts = (lead ? [lead] : []).concat(syls, trail ? [trail] : []);
  // glue punctuation onto their neighbours so it never stands alone
  const merged = [parts[0]];
  for (const piece of parts.slice(1)) {
    const hasWordChar = /\p{L}|\p{N}|_/u.test(piece);
    if (!hasWordChar && Array.from(piece).length <= 2 && merged.length > 1) {
      merged[merged.length - 1] += piece;
    } else {
      merged.push(piece);
    }
  }
  return merged.some((p) => p.trim()) ? merged : [word];
}

export interface SylPiece {
  text: string;
  /** Final piece of a word — a space belongs after it when another word
   * follows on the line. */
  wordEnd: boolean;
}

/** Split a whole lyric line into syllable pieces (matching the canonical
 * builder: the line is first tokenized like _elrc_split_words — CJK
 * characters are their own word — then each word is syllabified). */
export function syllabifyLine(line: string): SylPiece[] {
  const trimmed = (line ?? "").trim();
  if (!trimmed) return [];
  const words = splitElrcWords(trimmed);
  const out: SylPiece[] = [];
  words.forEach((tok, ti) => {
    const syls = syllabifyToken(tok);
    syls.forEach((s, si) => {
      out.push({ text: s, wordEnd: si === syls.length - 1 && ti < words.length - 1 });
    });
  });
  return out;
}

/** The line text a set of syllable pieces reassembles into. */
export function sylPiecesText(pieces: SylPiece[]): string {
  return pieces.map((p) => p.text + (p.wordEnd ? " " : "")).join("").replace(/\s+/g, " ").trim();
}
