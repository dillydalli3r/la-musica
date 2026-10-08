import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CloudDownload, PenLine, Plus, Trash2, Undo2, Keyboard } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import { LyricsKindChip } from "./Badges";
import Popover from "./Popover";
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
  /** The stamp TEXT as typed / parsed (`[mm:ss.xx]`, or bare digits in the
   *  editors' fields). Empty means "no text of its own" — an editor shows the
   *  line's own `time` through `fmtStamp` then, and a save always writes the
   *  formatted `time` regardless. */
  ts: string;
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

/** The DIGITS of a stamp at `decimals` — `mm:ss.xx`, the very text a saved
 *  line carries between its brackets. Every surface that SHOWS a time
 *  (the editor's own rows, its transport and the pending-stamp readout beside
 *  the slider) renders THIS, so what a reader sees while syncing is exactly
 *  what Save writes, decimals included, from the first stamped line on. */
export function fmtStamp(t: number, decimals = 2): string {
  // ponytail: integer total avoids 59.999->00:59.99 clamp; overflow carries
  const total = Math.max(0, Math.round(t * 10 ** decimals));
  const perMin = 60 * 10 ** decimals;
  const mm = Math.floor(total / perMin);
  const ss = Math.floor((total % perMin) / 10 ** decimals);
  const frac = total % 10 ** decimals;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}.${String(frac).padStart(decimals, "0")}`;
}

/** An LRC line / syllable stamp: those same digits in their brackets. */
export function fmtTs(t: number, decimals = 2): string {
  return `[${fmtStamp(t, decimals)}]`;
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

/** The KIND of a lyrics TEXT: "synced" when it carries timestamps, "plain"
 *  when it holds words without any, null when it holds no lyrics at all.
 *
 *  The stored kind of a TRACK is the payload's own `lyrics_kind` (the server
 *  reads the same two facts — presence and timestamps — off the stored files);
 *  this is the same question asked of the text an editor is holding, which is
 *  what the editor's own pane needs while its text is being stamped. There is
 *  no second detector here: presence is `hasLyricsText` and "carries
 *  timestamps" is `parseLrc`, which answers [] for a text with no timestamps
 *  at all (its own documented rule). */
export function lyricsKindOf(text: string | null | undefined): "synced" | "plain" | null {
  if (!hasLyricsText(text)) return null;
  return parseLrc(text ?? "").length ? "synced" : "plain";
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
  allowPlain,
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
  /** The user's `lyrics_allow_plain`: false makes the pane's "Plain" chip the
   *  failing mark (the same rule the track's own payload kind follows).
   *  Undefined while the config is unread — the neutral chip, no failure. */
  allowPlain?: boolean;
  /** Opens the full-screen enhanced editor when provided. */
  onEnhancedEditor?: () => void;
}) {
  const [lines, setLines] = useState<LrcLine[]>(() => parseLrc(initialLyrics));
  const [rawMode, setRawMode] = useState(false);
  const [raw, setRaw] = useState(initialLyrics);
  const [selIdx, setSelIdx] = useState(0);
  const [loading, setLoading] = useState(false);
  const [keysMenu, setKeysMenu] = useState(false);
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
  const [searchHits, setSearchHits] = useState<{ id: number; artist: string; track: string; duration?: number }[] | null>(null);

  // The host round-trips the text back in through this prop on every edit
  // (commit -> onChange -> parent state -> new initialLyrics), so resetting
  // here would wipe the undo stack after each change and Undo would always
  // answer "Nothing to undo". Reset only when the text came from somewhere
  // else — a different track, or a save/reload.
  const lastEmitted = useRef<string | null>(null);
  useEffect(() => {
    if (initialLyrics === lastEmitted.current) return;
    const parsed = parseLrc(initialLyrics);
    setLines(parsed);
    setRaw(initialLyrics);
    // Plain (untimed) text parses into NO lines, and the host hands the stored
    // text straight in (TrackPage seeds `initialLyrics` from its own
    // /api/tags read). Without this the pane fell through to "No lyrics yet…"
    // for a track whose plain lyrics were right there — uneditable, and
    // impossible to sync or replace. The raw editor is where untimed words
    // live until they are stamped.
    if (initialLyrics.trim() && !parsed.length) setRawMode(true);
    historyRef.current = [];
  }, [initialLyrics]);

  // The host may know only that lyrics EXIST (the import wizard's rows carry
  // the flags, not the text), so an empty initialLyrics means "ask the file".
  // Without this the panel announced "No lyrics yet" for a track whose lyrics
  // the chip next to it had just reported as present. Shares the ["tags", path]
  // cache entry with the track page, so this is one request.
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
      if (matchKey(e, keys, "prevLine")) {
        e.preventDefault();
        setSelIdx((s) => Math.max(0, s - 1));
      } else if (matchKey(e, keys, "nextLine")) {
        e.preventDefault();
        setSelIdx((s) => Math.min(lines.length - 1, s + 1));
      } else if (matchKey(e, keys, "undo")) {
        e.preventDefault();
        undo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selIdx, lines.length, keys, capturing, onSave]);

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

  return (
    <div
      data-lrc-editor
      className="bg-card rounded-lg border border-border p-4 flex flex-col"
    >
      <div className="flex items-center justify-between mb-3 flex-wrap gap-1.5">
        <div className="flex items-center gap-2 min-w-0">
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Lyrics</div>
          {/* Which KIND the text in this pane is — live, so entering a line's
              timestamp turns it into "Synced" before anything is saved. The
              track's own stored kind is the payload's (`lyrics_kind`), shown by
              the page header and the rows; this one describes the words being
              edited. */}
          <LyricsKindChip kind={lyricsKindOf(rawMode ? raw : serializeLrc(lines, dec))} allowPlain={allowPlain} size="sm" />
        </div>
        <div className="flex gap-1.5 flex-wrap">
          <button className="btn-ghost !py-1 text-xs" onClick={importFromProviders} disabled={loading}>
            <CloudDownload className="h-3.5 w-3.5" /> Auto-import
          </button>
          {onEnhancedEditor && (
            <button
              className="btn-ghost !py-1 text-xs"
              onClick={onEnhancedEditor}
              title="Full-screen enhanced editor — per-line timestamps, word/syllable splitting and auto-distribution"
            >
              <PenLine className="h-3.5 w-3.5" /> Enhanced
            </button>
          )}
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
              // Entering Raw from the line list seeds the textarea with that
              // list. A plain (untimed) text has no lines — serializeLrc is
              // "" — and reseeding would wipe the stored words the pane is
              // showing, so the text is kept when there is no line text.
              const text = serializeLrc(lines, dec);
              if (text.trim()) setRaw(text);
            }}
          >
            {rawMode ? "Lines" : "Raw"}
          </button>
        </div>
      </div>

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
              No lyrics yet. Auto-import lyrics from a provider, or switch to the{" "}
              <b>Raw</b> editor and type them.
            </>
          )}
        </div>
      ) : (
        <div className="flex-1 space-y-1 max-h-[420px] overflow-auto pr-1">
          {lines.map((l, i) => (
            <div
              key={i}
              className={`group flex items-center gap-2 rounded-lg border px-2 py-1.5 transition-colors ${
                i === selIdx
                  ? "border-accent/30 bg-panel"
                  : "border-transparent hover:border-border hover:bg-panel"
              }`}
              onClick={() => setSelIdx(i)}
            >
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
        <div className="flex gap-1 shrink-0">
          <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => shiftAll(-0.1)} title="Shift all timestamps 0.1s earlier">−0.1s</button>
          <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => shiftAll(0.1)} title="Shift all timestamps 0.1s later">+0.1s</button>
          <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={undo} title={`Undo (${keys.undo})`}>
            <Undo2 className="h-3 w-3" />
          </button>
        </div>
      </div>
      <div className="mt-1.5 text-[10px] text-zinc-600">
        {keys.prevLine}/{keys.nextLine} move between lines · {keys.undo} undo · {keys.save} save · click{" "}
        <Keyboard className="inline h-3 w-3" /> to rebind · timestamps format to {dec} decimals on save
      </div>
    </div>
  );
}
