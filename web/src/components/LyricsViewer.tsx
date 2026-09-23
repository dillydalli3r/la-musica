import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CloudDownload, PenLine, Play, Square, Plus, Trash2, Undo2, Keyboard, Upload } from "lucide-react";
import { api, isOffline } from "../api";
import { toast, useStore } from "../store";
import { playbackSource } from "../lib/mediaCache";
import LrclibPublishPanel from "./LrclibPublish";
import Popover from "./Popover";
import { nextSpeed, fmtSpeed } from "../lib/playback";
import { useLyricsFollow } from "../lib/lyrScroll";
import {
  loadLyricsKeys, saveLyricsKeys, resetLyricsKeys,
  keyLabel, matchKey, LYRICS_ACTIONS, LYRICS_KEY_DEFAULTS,
  type LyricsAction,
} from "../lib/lyricsKeys";

export interface LrcWord {
  time: number; // seconds
  text: string;
}

export interface LrcLine {
  ts: string; // [mm:ss.xx]
  time: number; // seconds
  text: string;
  words?: LrcWord[]; // ELRC inline word/syllable timestamps <mm:ss.xx>
  /** Untimed line borrowed into a mixed file (inherits previous timestamp). */
  untimed?: boolean;
  /** Syllable level: some word tags are glued together with no whitespace
   * ("<t>try<t>ing") — one tag per syllable instead of per word. */
  syl?: boolean;
}

const META_RE = /\[(?:ar|ti|al|au|la|by|re|ve|length|offset):[^\]]*\]/gi;
const TIME_RE = /\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;
const WORD_RE = /<(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?>/g;

function tsToTime(mm: string, ss: string, frac?: string): number {
  const f = (frac ?? "0").padEnd(3, "0").slice(0, 3);
  return parseInt(mm, 10) * 60 + parseInt(ss, 10) + parseInt(f, 10) / 1000;
}

export function fmtTs(t: number, decimals = 2): string {
  // ponytail: integer total avoids 59.999->00:59.99 clamp; overflow carries
  const total = Math.max(0, Math.round(t * 10 ** decimals));
  const perMin = 60 * 10 ** decimals;
  const mm = Math.floor(total / perMin);
  const ss = Math.floor((total % perMin) / 10 ** decimals);
  const frac = total % 10 ** decimals;
  return `[${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}.${String(frac).padStart(decimals, "0")}]`;
}

/** True when `text` really holds lyrics (mirrors backend has_lyrics_text):
 * not blank, not metadata headers only, and at least one line keeps text
 * after stripping tags + timestamps. A bare-[00:00.00]-only file is
 * lyric-less, not a line reading "[00:00.00]". */
export function hasLyricsText(text: string | null | undefined): boolean {
  if (!text || !text.trim()) return false;
  for (const line of text.split("\n")) {
    const body = line.replace(TIME_RE, "").replace(WORD_RE, "");
    if (body.replace(META_RE, "").trim()) return true;
  }
  return false;
}


export function parseLrc(lrc: string, shiftMs = 0): LrcLine[] {
  let offsetMs = 0;
  const off = lrc.match(/\[offset:\s*([+-]?\d+)\s*\]/i);
  if (off) offsetMs = parseInt(off[1], 10) || 0;
  // `shiftMs` is the reader's own nudge (the offset control), added to the
  // file's `[offset:]` header: the header is the FILE's correction, the
  // parameter the one the user is trying out before it is written back. Both
  // are millisecond shifts of the same sync, so they add — and with the
  // control at rest (0) this is the header alone, byte-for-byte what the
  // parser did before the parameter existed.
  const shift = (offsetMs + shiftMs) / 1000;
  const lines: LrcLine[] = [];
  let hasTimed = false;
  let lastTime = 0;
  for (const raw of lrc.split(/\r?\n/)) {
    // drop metadata tags like [ti:...] / [ar:...] (never lyric content)
    const cleaned = raw.replace(META_RE, "");
    const times = [...cleaned.matchAll(TIME_RE)];
    const body = cleaned.replace(TIME_RE, "");
    // ELRC word-level timestamps: <mm:ss.xx>word <mm:ss.xx>word2
    const parts = body.split(WORD_RE);
    const words: LrcWord[] = [];
    for (let k = 0; 4 * k + 3 < parts.length; k++) {
      const mm = parts[4 * k + 1];
      const ss = parts[4 * k + 2];
      if (mm === undefined || ss === undefined) break;
      // ponytail: negative [offset:] clamps at 0, no pre-echo seeks
      words.push({ time: Math.max(0, tsToTime(mm, ss, parts[4 * k + 3]) + shift), text: parts[4 * k + 4] ?? "" });
    }
    const text = words.length
      ? words.map((w) => w.text).join("").replace(/\s+/g, " ").trim()
      : body.trim();
    if (!text && !words.length) continue;
    // Syllable level: a piece that does NOT end in whitespace continues the
    // previous word (glued syllable tags); canonical word-level tags always
    // end their piece with a space (except the final one of the line).
    const syl = words.some((_w, k) => k > 0 && !/\s$/.test(words[k - 1].text));
    if (!times.length) {
      // Untimed line in a mixed file: sing with the previous line (stable
      // sort keeps file order within the cluster) instead of dropping it.
      // Pure-plain files return [] below, so plain panes keep working.
      // honey: inherits prev timestamp; per-line offsets need real stamps.
      lines.push({ ts: fmtTs(lastTime, 2), time: lastTime, text, untimed: true });
      continue;
    }
    hasTimed = true;
    for (const m of times) {
      const time = Math.max(0, tsToTime(m[1], m[2], m[3]) + shift);
      lastTime = time;
      lines.push({
        ts: fmtTs(time, 2),
        time,
        text,
        syl: syl || undefined,
        words: words.length ? words.map((w) => ({ ...w })) : undefined,
      });
    }
  }
  if (!hasTimed) return [];
  // sort + drop exact duplicates (a provider may repeat lines)
  lines.sort((a, b) => a.time - b.time);
  const seen = new Set<string>();
  return lines.filter((l) => {
    const key = `${l.time.toFixed(3)}|${l.text}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** parseLrc plus the clickable [00:00.00] leader: LRC parsing drops blank
 * lines, which leaves songs with an instrumental intro no top target —
 * synthesize one whenever the first real line arrives late. Both players'
 * lines and stored-transform seeding go through this, so their line
 * indexes always stay aligned. */
export function parsePlayerLrc(text: string, shiftMs = 0): LrcLine[] {
  const parsed = parseLrc(text, shiftMs);
  if (parsed.length && parsed[0].time > 0.35) {
    parsed.unshift({ ts: "[00:00.00]", time: 0, text: "" });
  }
  return parsed;
}

/** Seed stored xlit/trans arrays so index `i` matches main `displayLines[i]`:
 * synced transforms parse WITHOUT the time-0 leader (parseLrc, not
 * parsePlayerLrc), then take the main's leader when it has one — a plain
 * branch adds none either. Both panes share this, so no leader-shift. */
export function splitStoredLines(s: string, withLeader = false): string[] {
  // honey: blank-leader pad keeps xlit/trans 1:1; per-line realign if sources diverge.
  const out = /\[\d{1,2}:\d{1,2}/.test(s)
    ? parseLrc(s).map((l) => l.text)
    : s.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  if (withLeader && out.length) out.unshift("");
  return out;
}

/** The active line range, background vocals included: lines stamped at the
 * SAME moment (duets, backing vocals — within 50 ms) form one cluster and
 * are sung simultaneously, so they all highlight together. Returns
 * [first, last] indexes of the current cluster, [-1, -1] before the first
 * line. */
export function activeLineRange(lines: LrcLine[], t: number): [number, number] {
  let end = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].time <= t + 0.02) end = i;
    else break;
  }
  if (end < 0) return [-1, -1];
  let start = end;
  while (start > 0 && lines[start - 1].time >= lines[end].time - 0.05) start--;
  return [start, end];
}

/** Animated word/syllable karaoke sweep, shared by the fullscreen player,
 * the editor preview and the inline editor: the piece being sung pops
 * slightly with a soft glow, already-sung pieces stay lit, upcoming ones
 * stay dim. Each piece eases between states (color + transform), so the
 * sweep reads as motion instead of a hard swap. Works per word or per
 * syllable — the pieces carry their own granularity.
 *
 * Pieces are grouped into words first and each word is one unbreakable
 * inline-block: without that, a line break could land BETWEEN the
 * inline-block syllables of a single word and cut the word apart. */
export function KaraokeWords({
  words,
  time,
  currentClass = "text-accent scale-110 [text-shadow:0_0_16px_rgba(255,255,255,0.4)]",
  sungClass = "text-white",
  upcomingClass = "text-white/45",
}: {
  words: LrcWord[];
  time: number;
  currentClass?: string;
  sungClass?: string;
  upcomingClass?: string;
}) {
  // group the piece stream into words at the whitespace boundaries
  const wordGroups: LrcWord[][] = [];
  for (const w of words) {
    const last = wordGroups[wordGroups.length - 1];
    if (last && !/\s$/.test(last[last.length - 1].text)) last.push(w);
    else wordGroups.push([w]);
  }
  return (
    <>
      {wordGroups.map((group, gi) => {
        const trailing = /\s$/.test(group[group.length - 1].text);
        return (
          <span key={gi}>
            <span className="inline-block whitespace-nowrap">
              {group.map((w, wi) => {
                const sung = w.time <= time + 0.04;
                const nextT = wi < group.length - 1
                  ? group[wi + 1].time
                  : (wordGroups[gi + 1]?.[0]?.time ?? Infinity);
                const current = sung && nextT > time + 0.04;
                return (
                  <span
                    key={wi}
                    className={`inline-block transition-[color,transform,text-shadow] duration-200 ease-out ${
                      current ? currentClass : sung ? sungClass : upcomingClass
                    }`}
                    style={{ transformOrigin: "50% 75%" }}
                  >
                    {w.text.trimEnd()}
                  </span>
                );
              })}
            </span>
            {trailing ? " " : null}
          </span>
        );
      })}
    </>
  );
}

export function serializeLrc(lines: LrcLine[], decimals = 2): string {
  return lines
    .map((l) => {
      const ts = fmtTs(l.time, decimals);
      if (l.words?.length) {
        // pieces carry their own spacing (word-final pieces end with a
        // space, glued syllables don't) — concatenating reproduces the
        // line text exactly. No space after the line stamp (canonical).
        const body = l.words.map((w) => `<${fmtTs(w.time, decimals)}>${w.text}`).join("");
        return `${ts}${body}`;
      }
      return `${ts}${l.text}`;
    })
    .join("\n");
}

export default function LyricsViewer({
  path,
  initialLyrics,
  onChange,
  onSave,
  artist,
  track,
  album,
  duration,
  decimals = 2,
  staged = false,
  onEnhancedEditor,
}: {
  path: string;
  initialLyrics: string;
  onChange: (lrc: string) => void;
  onSave?: () => void;
  artist?: string;
  track?: string;
  album?: string;
  duration?: number;
  decimals?: number;
  /** The track is in an album the import wizard is editing that is not in the
   *  library yet, so the read asks the server for its staged allowance. */
  staged?: boolean;
  /** Opens the full-screen enhanced editor (syllable tap-sync, playback
   * speed) when provided. */
  onEnhancedEditor?: () => void;
}) {
  const [lines, setLines] = useState<LrcLine[]>(() => parseLrc(initialLyrics));
  const [rawMode, setRawMode] = useState(false);
  const [raw, setRaw] = useState(initialLyrics);
  const [playing, setPlaying] = useState(false);
  const [playTime, setPlayTime] = useState(0);
  const [speed, setSpeed] = useState(1);
  // Field selector, not `useStore()`: a selector-less call re-rendered this
  // whole pane — the scroller and every lyric row — on every store write,
  // volume drag steps included.
  const vol = useStore((s) => s.vol);
  const [dur, setDur] = useState(duration ?? 0);
  const [selIdx, setSelIdx] = useState(0);
  const [loading, setLoading] = useState(false);
  const [keysMenu, setKeysMenu] = useState(false);
  const [pubOpen, setPubOpen] = useState(false);
  const [capturing, setCapturing] = useState<LyricsAction | null>(null);
  const [keys, setKeys] = useState(() => loadLyricsKeys());
  // Where the host page's Save writes: embedded tag, .lrc sidecar, or both.
  const [saveTarget, setSaveTarget] = useState<"embedded" | "sidecar" | "both">(
    () => (localStorage.getItem("mlo.lyricsSaveTarget") as "embedded" | "sidecar" | "both") ?? "embedded"
  );
  // Timestamp precision used on save — 2 or 3 decimals (persisted).
  const [dec, setDec] = useState<number>(() => {
    const v = Number(localStorage.getItem("mlo.lyricsDecimals"));
    return v === 2 || v === 3 ? v : decimals;
  });
  const historyRef = useRef<LrcLine[][]>([]);
  const pendingWords = useRef<{ idx: number; parts: string[]; times: number[]; done: number } | null>(null);
  const [searchHits, setSearchHits] = useState<{ id: number; artist: string; track: string; duration?: number }[] | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  // The track the preview element's source was last (or is being) resolved
  // for: a cache lookup that lands after a track change must not overwrite
  // the newer one's src.
  const previewPath = useRef<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const lineRefs = useRef<Record<number, HTMLDivElement | null>>({});

  // The host round-trips the text back in through this prop on every edit
  // (commit -> onChange -> parent state -> new initialLyrics), so resetting
  // here would wipe the undo stack after each change and Undo would always
  // answer "Nothing to undo". Reset only when the text came from somewhere
  // else — a different track, or a save/reload.
  const lastEmitted = useRef<string | null>(null);
  useEffect(() => {
    if (initialLyrics === lastEmitted.current) return;
    setLines(parseLrc(initialLyrics));
    setRaw(initialLyrics);
    historyRef.current = [];
  }, [initialLyrics]);

  // The host may know only that lyrics EXIST (the import wizard's rows carry
  // the flags, not the text), so an empty initialLyrics means "ask the file".
  // Without this the panel announced "No lyrics yet" for a track whose lyrics
  // the chip next to it had just reported as present. Shares the ["tags", path]
  // cache entry with the player bar / track page, so this is one request.
  // A FAILED read is an error with its reason — never an empty pane that reads
  // as "this file has no lyrics".
  const { data: stored, isPending: storedPending, error: storedError } = useQuery({
    queryKey: ["tags", path],
    queryFn: () => api.tags(path, staged),
    enabled: !!path && !initialLyrics.trim(),
    retry: false,
  });

  useEffect(() => {
    if (initialLyrics.trim() || !stored) return;
    const text = typeof stored.lyrics === "string" ? stored.lyrics : "";
    if (!text.trim()) return;
    const parsed = parseLrc(text);
    setLines(parsed);
    setRaw(text);
    // Plain-text lyrics carry no timestamps, so there is no line list to show.
    // Open the raw editor rather than claiming the track has no lyrics.
    if (!parsed.length) setRawMode(true);
    historyRef.current = [];
  }, [stored, initialLyrics]);

  // Active line derived from playback time (never conflated with the index).
  const activeLine = useMemo(() => {
    let idx = -1;
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].time <= playTime + 0.02) idx = i;
      else break;
    }
    return idx;
  }, [playTime, lines]);

  // Follow the playing line. The pane scrolls ITSELF: scrollIntoView walks
  // every scrollable ancestor, so the editor's list used to drag the whole
  // page it sits in along with it. Centring (0.5) reads better than the
  // player's upper-third anchor on a short editing list.
  const { takeOver } = useLyricsFollow({
    active: activeLine,
    time: playTime,
    playing,
    scroll: listRef,
    rows: lineRefs,
    reset: path,
    anchor: 0.5,
  });

  // The preview is a second decoder: the player bar's volume effect only
  // reaches its own elements, so this one follows the shared app volume
  // (and the speed bindings) itself.
  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    a.playbackRate = speed;
    a.volume = vol;
  }, [speed, vol, playing]);

  // A track change abandons a source still resolving for the old one.
  useEffect(() => {
    previewPath.current = null;
  }, [path]);

  const emit = (ls: LrcLine[]) => {
    const text = serializeLrc(ls, dec);
    // Remember what left this component: the host echoes it straight back
    // through initialLyrics, and that echo must not reset the undo history.
    lastEmitted.current = text;
    onChange(text);
  };

  /** Every mutating path goes through commit() so Undo works. */
  const commit = (next: LrcLine[]) => {
    historyRef.current.push(lines);
    if (historyRef.current.length > 100) historyRef.current.shift();
    setLines(next);
    emit(next);
  };

  const undo = () => {
    const prev = historyRef.current.pop();
    if (!prev) {
      toast("Nothing to undo");
      return;
    }
    setLines(prev);
    emit(prev);
    setSelIdx((s) => Math.min(s, Math.max(0, prev.length - 1)));
  };

  /** Keep ELRC word stamps alive across a text edit: when the edited text
   * still tokenizes into the same number of words, re-map the old timings
   * onto the new tokens instead of dropping them. */
  const remapWords = (l: LrcLine, newText: string): LrcWord[] | undefined => {
    if (!l.words?.length) return undefined;
    const tokens = newText.trim().split(/\s+/).filter(Boolean);
    if (tokens.length !== l.words.length) return undefined;
    return l.words.map((w, i) => ({ ...w, text: tokens[i] }));
  };

  const updateLine = (i: number, patch: Partial<LrcLine>) => {
    const next = lines.map((l, j) => {
      if (j !== i) return l;
      // manual text edits keep word timings when the token count still matches
      if ("text" in patch) return { ...l, ...patch, words: remapWords(l, patch.text ?? "") };
      return { ...l, ...patch };
    });
    commit(next);
  };

  const addLine = (i: number) => {
    const base = lines[i]?.time ?? lines[lines.length - 1]?.time ?? 0;
    const next = [...lines];
    next.splice(i + 1, 0, { ts: "[00:00.00]", time: base, text: "" });
    commit(next);
    setSelIdx(i + 1);
  };

  const removeLine = (i: number) => {
    const next = lines.filter((_, j) => j !== i);
    commit(next);
    setSelIdx((s) => Math.max(0, Math.min(s, next.length - 1)));
  };

  /** Shift every timestamp by ±delta seconds (fix whole-track drift). */
  const shiftAll = (delta: number) => {
    const next = lines.map((l) => {
      const time = Math.max(0, l.time + delta);
      const out: LrcLine = { ...l, time, ts: fmtTs(time, dec) };
      if (out.words?.length) out.words = out.words.map((w) => ({ ...w, time: Math.max(0, w.time + delta) }));
      return out;
    });
    commit(next);
    toast(`${delta > 0 ? "+" : ""}${delta.toFixed(1)}s applied to all lines`);
  };

  const togglePlay = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
      setPlaying(false);
    } else {
      const p = path;
      // The same resolution the player bar uses (lib/mediaCache): a downloaded
      // track previews in a shell with no server, and `playback_source`
      // decides which copy plays while both are available. The lookup is
      // async, so the guard below keeps a slow one from pointing the element
      // at the track we have since left.
      previewPath.current = p;
      void (async () => {
        const source = await playbackSource(p);
        if (previewPath.current !== p) return;
        if (!source.cached && isOffline()) {
          toast.error(`“${track || p}” isn’t downloaded — it needs the server to play.`);
        }
        audio.src = source.src;
        audio.playbackRate = speed;
        audio.volume = vol;
        audio.play().catch(() => toast("Playback failed — audio format unsupported in browser"));
      })();
      setPlaying(true);
    }
  };

  /** Step the PREVIEW's rate — the hotkeys advertised in the keys menu are
   * the editor's, not the main player's. */
  const stepSpeed = (dir: 1 | -1) => {
    const next = nextSpeed(speed, dir);
    setSpeed(next);
    if (audioRef.current) audioRef.current.playbackRate = next;
    toast(`Preview speed ${fmtSpeed(next)}`);
  };

  const seekTo = (time: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = time;
    setPlayTime(time);
  };

  // ---- ELRC word stamping ------------------------------------------------
  // Word stamps accumulate in a ref while the line is being sung; the ELRC
  // word list is only attached once every word has a timestamp, so partial
  // stamping never serializes zeros into the saved lyrics.
  const stampWord = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (!playing) {
      toast("Press Play first, then stamp words while the song plays");
      return;
    }
    const idx = Math.max(0, Math.min(selIdx, lines.length - 1));
    const line = lines[idx];
    if (!line) return;
    const parts = line.text.trim().split(/\s+/).filter(Boolean);
    if (!parts.length) {
      toast("Type the lyric text first, then stamp its words");
      return;
    }
    if (
      !pendingWords.current ||
      pendingWords.current.idx !== idx ||
      pendingWords.current.parts.join("\u0000") !== parts.join("\u0000")
    ) {
      pendingWords.current = { idx, parts, times: parts.map(() => 0), done: 0 };
    }
    const pending = pendingWords.current;
    if (pending.done >= parts.length) {
      toast("All words stamped — select the next line");
      return;
    }
    const t = Math.max(0, audio.currentTime - 0.05);
    pending.times[pending.done] = t;
    pending.done += 1;
    setPlayTime(t);
    if (pending.done >= parts.length) {
      const next = lines.map((l, j) =>
        j === idx ? { ...l, words: parts.map((text, wi) => ({ text, time: pending.times[wi] })) } : l
      );
      commit(next);
      pendingWords.current = null;
      toast("Line word-synced ✓ — select the next line");
    } else {
      toast(`Word ${pending.done}/${parts.length} stamped`);
    }
  };

  const stampLine = () => {
    const audio = audioRef.current;
    if (!audio || !playing) {
      toast("Press Play first, then stamp each line's time");
      return;
    }
    const t = Math.max(0, audio.currentTime - 0.05);
    const target = activeLine >= 0 ? activeLine : selIdx;
    const idx = Math.min(target, Math.max(0, lines.length - 1));
    const next = lines.map((l, j) => (j === idx ? { ...l, time: t, ts: fmtTs(t, dec) } : l));
    commit(next);
    setSelIdx((s) => Math.min(s + 1, Math.max(0, lines.length - 1)));
    setPlayTime(t);
  };

  // ---- Customizable hotkeys ----------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Capturing a new binding always wins (works from any focus).
      if (capturing) {
        e.preventDefault();
        if (e.code === "Escape") {
          setCapturing(null);
          return;
        }
        const label = keyLabel(e);
        const next = { ...keys };
        for (const a of Object.keys(next) as LyricsAction[]) {
          if (next[a] === label) next[a] = LYRICS_KEY_DEFAULTS[a];
        }
        next[capturing] = label;
        setKeys(next);
        saveLyricsKeys(next);
        setCapturing(null);
        return;
      }

      const t = e.target as HTMLElement;
      const isLyricTextInput =
        t instanceof HTMLInputElement && t.dataset.lyrictext !== undefined;
      const isOtherInput =
        (t instanceof HTMLInputElement && t.dataset.lyrictext === undefined) ||
        t instanceof HTMLTextAreaElement ||
        t.isContentEditable;

      if (matchKey(e, keys, "save")) {
        e.preventDefault();
        onSave?.();
        return;
      }
      if (isLyricTextInput || isOtherInput) return; // don't hijack typing
      if (matchKey(e, keys, "stampLine")) {
        e.preventDefault();
        stampLine();
      } else if (matchKey(e, keys, "stampWord")) {
        e.preventDefault();
        stampWord();
      } else if (matchKey(e, keys, "playPause")) {
        e.preventDefault();
        togglePlay();
      } else if (matchKey(e, keys, "seekBack")) {
        e.preventDefault();
        seekTo(Math.max(0, playTime - 2));
      } else if (matchKey(e, keys, "seekForward")) {
        e.preventDefault();
        seekTo(playTime + 2);
      } else if (matchKey(e, keys, "prevLine")) {
        e.preventDefault();
        setSelIdx((s) => Math.max(0, s - 1));
      } else if (matchKey(e, keys, "nextLine")) {
        e.preventDefault();
        setSelIdx((s) => Math.min(lines.length - 1, s + 1));
      } else if (matchKey(e, keys, "undo")) {
        e.preventDefault();
        undo();
      } else if (matchKey(e, keys, "speedSlower")) {
        e.preventDefault();
        stepSpeed(-1);
      } else if (matchKey(e, keys, "speedFaster")) {
        e.preventDefault();
        stepSpeed(1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, selIdx, lines, activeLine, playTime, keys, capturing, onSave, speed]);

  // ---- provider import -----------------------------------------------------
  const importFromProviders = async () => {
    if (!artist || !track) {
      toast("Track needs ARTIST and TITLE tags first");
      return;
    }
    setLoading(true);
    setSearchHits(null);
    try {
      const res = await api.lyricsGet(artist, track, album, duration);
      const lrc = res?.syncedLyrics ?? res?.plainLyrics;
      if (!lrc) {
        const hits = await api.lyricsSearch(artist, track, album, duration);
        setSearchHits(
          Array.isArray(hits) && hits.length
            ? hits.map((h) => ({ id: h.id, artist: String(h.artist ?? ""), track: String(h.track ?? ""), duration: h.duration ? Number(h.duration) : undefined }))
            : []
        );
        if (!hits?.length) toast("No lyrics found in any provider");
        return;
      }
      const from = res?.provider_label ? `Imported from ${res.provider_label}` : "Imported";
      applyImport(lrc, from);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  };

  const applyImport = (lrc: string, message: string) => {
    const parsed = parseLrc(lrc);
    historyRef.current.push(lines);
    setLines(parsed);
    setRaw(lrc);
    emit(parsed);
    setSelIdx(0);
    pendingWords.current = null;
    const wordCount = parsed.reduce((n, l) => n + (l.words?.length ? 1 : 0), 0);
    toast(
      wordCount
        ? `${message} — ${parsed.length} lines, ${wordCount} word-synced (ELRC)`
        : `${message} — ${parsed.length} lines`,
    );
  };

  const importSearchHit = async (hit: { artist: string; track: string; duration?: number }) => {
    setSearchHits(null);
    setLoading(true);
    try {
      const res = await api.lyricsGet(hit.artist, hit.track, undefined, hit.duration);
      const lrc = res?.syncedLyrics ?? res?.plainLyrics;
      if (!lrc) {
        toast("That result has no lyrics");
        return;
      }
      applyImport(lrc, `Imported "${hit.artist} — ${hit.track}"`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLoading(false);
    }
  };

  const fmtDur = (t: number) => {
    const m = Math.floor(t / 60);
    const s = Math.floor(t % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  return (
    <div data-lrc-editor className="bg-card rounded-lg border border-border p-4 flex flex-col">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-1.5">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Lyrics</div>
        <div className="flex gap-1.5 flex-wrap">
          <button className="btn-ghost !py-1 text-xs" onClick={togglePlay}>
            {playing ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            {playing ? "Stop" : "Preview"}
          </button>
          <button className="btn-ghost !py-1 text-xs" onClick={importFromProviders} disabled={loading}>
            <CloudDownload className="h-3.5 w-3.5" /> Auto-import
          </button>
          {onEnhancedEditor && (
            <button
              className="btn-ghost !py-1 text-xs"
              onClick={onEnhancedEditor}
              title="Full-screen enhanced editor — syllable tap-sync along the vocals, playback speed"
            >
              <PenLine className="h-3.5 w-3.5" /> Enhanced
            </button>
          )}
          <button
            className={`btn-ghost !py-1 text-xs ${(rawMode ? raw : serializeLrc(lines, dec)).trim() ? "" : "opacity-40"} ${pubOpen ? "!text-accent" : ""}`}
            onClick={() => setPubOpen(!pubOpen)}
            title="Submit these lyrics to the LRCLIB community database"
          >
            <Upload className="h-3.5 w-3.5" /> Publish
          </button>
          {onSave && (
            <select
              className="input !py-1 !px-1.5 text-xs w-auto"
              value={saveTarget}
              title="Where the Save button writes lyrics"
              onChange={(e) => {
                const v = e.target.value as "embedded" | "sidecar" | "both";
                setSaveTarget(v);
                localStorage.setItem("mlo.lyricsSaveTarget", v);
              }}
            >
              <option value="embedded">Save → tag</option>
              <option value="sidecar">Save → .lrc</option>
              <option value="both">Save → tag + .lrc</option>
            </select>
          )}
          <select
            className="input !py-1 !px-1.5 text-xs w-auto"
            value={dec}
            title="Timestamp precision used when saving"
            onChange={(e) => {
              const v = Number(e.target.value);
              setDec(v);
              localStorage.setItem("mlo.lyricsDecimals", String(v));
            }}
          >
            <option value={2}>2 dec</option>
            <option value={3}>3 dec</option>
          </select>
          <div className="relative">
            <button className="btn-ghost !py-1 text-xs" onClick={() => setKeysMenu(!keysMenu)} title="Keyboard shortcuts">
              <Keyboard className="h-3.5 w-3.5" />
            </button>
            <Popover open={keysMenu} onClose={() => setKeysMenu(false)} panelClass="w-80 p-1.5">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 px-1 pb-1">Hotkeys</div>
                {LYRICS_ACTIONS.map((a) => (
                  <div key={a.id} className="flex items-center gap-2 py-0.5">
                    <span className="flex-1 text-xs text-zinc-300" title={a.hint}>{a.label}</span>
                    <button
                      className={`chip text-[10px] font-mono border ${capturing === a.id ? "bg-accent/20 border-accent text-accent" : "bg-raise border-border text-zinc-400 hover:border-accent"}`}
                      onClick={() => setCapturing(capturing === a.id ? null : a.id)}
                      title="Click, then press the new key combination (Esc cancels)"
                    >
                      {capturing === a.id ? "press key…" : keys[a.id]}
                    </button>
                  </div>
                ))}
                <div className="flex justify-between items-center mt-2 pt-1.5 border-t border-border">
                  <button
                    className="text-[10px] text-zinc-500 hover:text-zinc-300 px-1"
                    onClick={() => {
                      resetLyricsKeys();
                      setKeys(loadLyricsKeys());
                      toast("Hotkeys reset to defaults");
                    }}
                  >
                    reset to defaults
                  </button>
                  <span className="text-[10px] text-zinc-600 px-1">saved in this browser</span>
                </div>
            </Popover>
          </div>
          <button
            className="btn-ghost !py-1 text-xs"
            onClick={() => {
              setRawMode(!rawMode);
              setRaw(serializeLrc(lines, dec));
            }}
          >
            {rawMode ? "Lines" : "Raw"}
          </button>
        </div>
      </div>

      <audio
        ref={audioRef}
        onTimeUpdate={(e) => setPlayTime(e.currentTarget.currentTime)}
        onLoadedMetadata={(e) => setDur(e.currentTarget.duration || 0)}
        onEnded={() => setPlaying(false)}
        className="hidden"
      />

      {pubOpen && (
        <div className="rounded-md border border-border bg-panel p-3 mb-2 space-y-2">
          <div className="text-[11px] text-zinc-400 font-medium">Publish to LRCLIB</div>
          <LrclibPublishPanel
            artist={artist ?? ""}
            track={track ?? ""}
            album={album}
            duration={Math.round(dur || duration || 0)}
            text={rawMode ? raw : serializeLrc(lines, dec)}
            onDone={() => setPubOpen(false)}
          />
        </div>
      )}

      {searchHits && (
        <div className="rounded-md border border-border bg-panel p-3 mb-2">
          <div className="text-[11px] text-zinc-400 mb-1.5">
            {searchHits.length ? "Multiple matches — pick one:" : "No exact match — nothing found in any provider."}
          </div>
          {searchHits.length > 0 && (
            <div className="space-y-1 max-h-40 overflow-auto">
              {searchHits.map((h) => (
                <div key={h.id} className="flex items-center gap-2 text-xs">
                  <span className="flex-1 truncate text-zinc-300">
                    {h.artist} — <span className="text-zinc-400">{h.track}</span>
                  </span>
                  <button className="btn-ghost !py-0.5 text-[11px]" onClick={() => importSearchHit(h)}>
                    Import
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {rawMode ? (
        <textarea
          className="input flex-1 font-mono text-xs min-h-[300px]"
          value={raw}
          onChange={(e) => {
            setRaw(e.target.value);
            lastEmitted.current = e.target.value;
            onChange(e.target.value);
          }}
        />
      ) : lines.length === 0 ? (
        <div className="text-sm text-zinc-500 py-10 text-center">
          {storedPending && !initialLyrics.trim() ? (
            "Reading the file's lyrics…"
          ) : storedError ? (
            <span className="text-red-300" role="alert">
              Could not read this file's lyrics — {storedError instanceof Error ? storedError.message : String(storedError)}.
              <br />
              The file's own lyrics may still be fine; only this read failed.
            </span>
          ) : (
            <>
              No lyrics yet. Press <kbd className="chip bg-raise border border-border">Play</kbd>, then select a line and
              press <kbd className="chip bg-raise border border-border">{keys.stampLine}</kbd> on each line to stamp its
              timestamp — or auto-import lyrics from a provider.
            </>
          )}
        </div>
      ) : (
        <div
          ref={listRef}
          className="flex-1 space-y-1 max-h-[420px] overflow-auto pr-1"
          onWheel={(e) => {
            if (e.deltaY !== 0) takeOver();
          }}
          onTouchStart={takeOver}
        >
          {lines.map((l, i) => (
            <div
              key={i}
              ref={(el) => {
                lineRefs.current[i] = el;
              }}
              className={`group flex items-center gap-2 rounded-lg border px-2 py-1.5 transition-colors ${
                i === activeLine && playing
                  ? "border-accent/60 bg-accent/20"
                  : i === selIdx
                    ? "border-accent/30 bg-panel"
                    : "border-transparent hover:border-border hover:bg-panel"
              }`}
              onClick={() => {
                if (i !== selIdx) pendingWords.current = null;
                setSelIdx(i);
              }}
            >
              <button
                className="p-0.5 text-zinc-600 hover:text-accent-soft shrink-0"
                title={`Seek to ${l.ts}`}
                onClick={(e) => {
                  e.stopPropagation();
                  seekTo(l.time);
                }}
              >
                <Play className="h-3 w-3" />
              </button>
              <input
                className="w-[86px] bg-transparent font-mono text-xs text-zinc-400 outline-none border border-transparent focus:border-accent rounded px-1 py-0.5"
                value={l.ts}
                onChange={(e) => {
                  const m = e.target.value.match(/(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?/);
                  if (!m) return;
                  const time =
                    parseInt(m[1], 10) * 60 +
                    parseInt(m[2], 10) +
                    parseInt((m[3] ?? "0").padEnd(2, "0").slice(0, 2), 10) / 100;
                  updateLine(i, { ts: e.target.value, time });
                }}
              />
              {i === activeLine && playing && l.words?.length ? (
                <span className="flex-1 text-sm px-1">
                  <KaraokeWords
                    words={l.words}
                    time={playTime}
                    currentClass="text-accent font-semibold scale-110"
                    sungClass="text-zinc-200"
                    upcomingClass="text-zinc-500"
                  />
                </span>
              ) : (
                <div className="flex-1 flex items-center gap-1.5 min-w-0">
                  <input
                    data-lyrictext
                    className="flex-1 bg-transparent text-sm text-zinc-200 outline-none border border-transparent focus:border-accent rounded px-1 py-0.5 min-w-0"
                    value={l.text}
                    placeholder="Lyric line…"
                    onChange={(e) => updateLine(i, { text: e.target.value })}
                  />
                  {l.words?.length ? (
                    <span
                      className={`chip text-[9px] shrink-0 border ${
                        l.syl
                          ? "bg-accent/10 border-accent/25 text-accent-soft"
                          : "bg-white/5 border-white/15 text-zinc-400"
                      }`}
                      title={l.syl ? "Syllable-synced (glued ELRC tags)" : "Word-synced (ELRC)"}
                    >
                      {l.words.length}{l.syl ? "s" : "w"}
                    </span>
                  ) : null}
                </div>
              )}
              <div className="opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 flex gap-1 transition-opacity shrink-0">
                <button className="text-zinc-500 hover:text-accent-soft" onClick={(e) => { e.stopPropagation(); addLine(i); }} title="Add line after">
                  <Plus className="h-3.5 w-3.5" />
                </button>
                <button className="text-zinc-500 hover:text-red-400" onClick={(e) => { e.stopPropagation(); removeLine(i); }} title="Remove line">
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-2 flex items-center gap-2">
        <span className="text-[10px] font-mono text-zinc-500 w-10 text-right shrink-0">{fmtDur(playTime)}</span>
        <input
          type="range"
          min={0}
          max={dur || 0}
          step={0.05}
          value={Math.min(playTime, dur || 0)}
          onChange={(e) => seekTo(Number(e.target.value))}
          className="flex-1 "
          title="Seek within the track"
        />
        <span className="text-[10px] font-mono text-zinc-500 w-10 shrink-0">{fmtDur(dur || 0)}</span>
        <div className="flex gap-1 shrink-0">
          <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => shiftAll(-0.1)} title="Shift all timestamps 0.1s earlier">−0.1s</button>
          <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => shiftAll(0.1)} title="Shift all timestamps 0.1s later">+0.1s</button>
          <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={undo} title={`Undo (${keys.undo})`}>
            <Undo2 className="h-3 w-3" />
          </button>
        </div>
      </div>
      <div className="mt-1.5 text-[10px] text-zinc-600">
        {keys.stampLine} stamps the <b>line being sung</b> and advances · {keys.stampWord} stamps word-by-word (ELRC) ·
        {" "}{keys.playPause} play/pause · {keys.seekBack}/{keys.seekForward} seek · click <Keyboard className="inline h-3 w-3" /> to rebind ·
        ▶ seeks to a line · timestamps format to {dec} decimals on save
      </div>
    </div>
  );
}
