import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Eraser, Keyboard, Languages, Loader2, Pause, Play, Plus, Save,
  Trash2, Undo2, Wand2, X,
} from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import {
  parseLrc, serializeLrc, KaraokeWords, type LrcLine, type LrcWord,
} from "./LyricsViewer";
import {
  loadLyricsKeys, saveLyricsKeys, resetLyricsKeys,
  keyLabel, matchKey, LYRICS_ACTIONS, LYRICS_KEY_DEFAULTS,
  type LyricsAction,
} from "../lib/lyricsKeys";
import { syllabifyLine } from "../lib/syllables";
import { SPEEDS, fmtSpeed } from "../lib/playback";

type StampMode = "line" | "word" | "syllable";

const fmtDur = (t: number) => {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
};

/** Parse stored lyrics into editor lines; plain text becomes untimed
 * lines ready for stamping. */
function toLines(text: string): LrcLine[] {
  const trimmed = (text ?? "").trim();
  if (!trimmed) return [];
  const parsed = parseLrc(trimmed);
  if (parsed.length) return parsed;
  return trimmed.split(/\r?\n/).map((ln) => ln.trim()).filter(Boolean)
    .map((text) => ({ ts: "[00:00.00]", time: 0, text }));
}

/** The next syllable/word pending-stamp state: pieces + collected times,
 * committed to the line only once every piece has a time. */
interface Pending {
  idx: number;
  key: string;
  pieces: string[]; // text pieces incl. their trailing space
  times: number[];
  done: number;
}

/** Enhanced lyric editor / creator: stamp line, word and SYLLABLE times
 * along the vocals (at any playback speed), auto-distribute word/syllable
 * times inside stamped lines, romanize foreign scripts for tagging,
 * and save into the LYRICS tag / .lrc sidecar. The LYRICS field itself
 * always keeps the original language — romanization is stored separately
 * (TRANSLITERATION-<lang>-LATN), exactly like the Lyrics Translate
 * script. */
export default function LyricsEditorModal({
  path,
  artist,
  track,
  album,
  duration,
  initialLyrics,
  onClose,
  onSaved,
}: {
  path: string;
  artist?: string;
  track?: string;
  album?: string;
  duration?: number;
  initialLyrics: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const [lines, setLines] = useState<LrcLine[]>(() => toLines(initialLyrics));
  const [draft, setDraft] = useState("");
  const [selIdx, setSelIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [playTime, setPlayTime] = useState(0);
  const [dur, setDur] = useState(duration ?? 0);  const [speed, setSpeed] = useState(1);
  const [mode, setMode] = useState<StampMode>("line");
  const [busy, setBusy] = useState<string | null>(null);
  const [keysMenu, setKeysMenu] = useState(false);
  const [capturing, setCapturing] = useState<LyricsAction | null>(null);
  const [keys, setKeys] = useState(() => loadLyricsKeys());
  const [xlitLines, setXlitLines] = useState<string[] | null>(null);
  const [saveTarget, setSaveTarget] = useState<"embedded" | "sidecar" | "both">(
    () => (localStorage.getItem("mlo.lyricsSaveTarget") as "embedded" | "sidecar" | "both") ?? "embedded"
  );
  const [dec, setDec] = useState<number>(() => {
    const v = Number(localStorage.getItem("mlo.lyricsDecimals"));
    return v === 2 || v === 3 ? v : 2;
  });
  const historyRef = useRef<LrcLine[][]>([]);
  const pendingRef = useRef<Pending | null>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Record<number, HTMLDivElement | null>>({});

  useEffect(() => {
    setLines(toLines(initialLyrics));
    historyRef.current = [];
    pendingRef.current = null;
  }, [initialLyrics]);

  // Keep the selected row visible while navigating / stamping.
  useEffect(() => {
    rowRefs.current[selIdx]?.scrollIntoView({ block: "nearest" });
  }, [selIdx]);

  // Playback rate follows the speed control (pitch preserved).
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = speed;
  }, [speed, playing]);

  // Smooth clock while playing.
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const tick = () => {
      const a = audioRef.current;
      if (a) setPlayTime(a.currentTime);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  const activeLine = useMemo(() => {
    let idx = -1;
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].time <= playTime + 0.02) idx = i;
      else break;
    }
    return idx;
  }, [lines, playTime]);

  const commit = (next: LrcLine[]) => {
    historyRef.current.push(lines);
    if (historyRef.current.length > 100) historyRef.current.shift();
    setLines(next);
  };
  const undo = () => {
    const prev = historyRef.current.pop();
    if (!prev) {
      toast("Nothing to undo");
      return;
    }
    setLines(prev);
    pendingRef.current = null;
  };

  const audio = () => audioRef.current;

  const togglePlay = () => {
    const a = audio();
    if (!a) return;
    if (playing) {
      a.pause();
      setPlaying(false);
    } else {
      if (!a.src) a.src = api.streamUrl(path);
      a.playbackRate = speed;
      a.play().catch(() => toast("Playback failed — audio format unsupported in browser"));
      setPlaying(true);
    }
  };

  const seekTo = (t: number) => {
    const a = audio();
    if (!a) return;
    a.currentTime = Math.max(0, t);
    setPlayTime(a.currentTime);
  };

  const stepSpeed = (dir: 1 | -1) => {
    const i = SPEEDS.indexOf(speed);
    const next = SPEEDS[Math.min(SPEEDS.length - 1, Math.max(0, (i < 0 ? SPEEDS.indexOf(1) : i) + dir))];
    setSpeed(next);
  };

  /** Start (or continue) a pending word/syllable stamp run for a line. */
  const pendingFor = (idx: number, pieces: string[]): Pending => {
    const p = pendingRef.current;
    const key = pieces.join("\u0000");
    if (p && p.idx === idx && p.key === key) return p;
    const fresh: Pending = { idx, key, pieces, times: pieces.map(() => 0), done: 0 };
    pendingRef.current = fresh;
    return fresh;
  };

  const stampLine = () => {
    const a = audio();
    if (!a || !playing) {
      toast("Press Play first, then stamp each line's time");
      return;
    }
    if (!lines.length) {
      toast("Add or paste lyric lines first");
      return;
    }
    const t = Math.max(0, a.currentTime - 0.05);
    const idx = Math.min(Math.max(selIdx, 0), lines.length - 1);
    commit(lines.map((l, j) => (j === idx ? { ...l, time: t, ts: "" } : l)));
    setSelIdx(Math.min(idx + 1, lines.length - 1));
    setPlayTime(t);
  };

  /** Shared word/syllable tap-stamping: pieces carry their own spacing
   * (a word-final piece ends with a space, glued syllables don't), so the
   * committed word list re-serializes to canonical ELRC. */
  const stampPieces = (kind: "word" | "syllable") => {
    const a = audio();
    if (!a || !playing) {
      toast("Press Play first, then tap along with the vocals");
      return;
    }
    const idx = Math.min(Math.max(selIdx, 0), lines.length - 1);
    const line = lines[idx];
    if (!line?.text.trim()) {
      toast("Type the lyric text first, then stamp it");
      return;
    }
    let pieces: string[];
    if (kind === "syllable") {
      pieces = syllabifyLine(line.text).map((s) => s.text + (s.wordEnd ? " " : ""));
    } else {
      pieces = line.text.trim().split(/\s+/).map((w, i, arr) => (i < arr.length - 1 ? `${w} ` : w));
    }
    if (!pieces.length) return;
    const p = pendingFor(idx, pieces);
    if (p.done >= p.pieces.length) {
      toast(`${kind === "syllable" ? "Syllables" : "Words"} complete — select the next line`);
      return;
    }
    const t = Math.max(0, a.currentTime - 0.05);
    p.times[p.done] = t;
    p.done += 1;
    setPlayTime(t);
    if (p.done >= p.pieces.length) {
      const words: LrcWord[] = p.pieces.map((text, i) => ({ time: p.times[i], text }));
      commit(lines.map((l, j) => (j === idx ? { ...l, words } : l)));
      pendingRef.current = null;
      toast(`${kind === "syllable" ? "Syllable" : "Word"}-synced ✓ — next line selected`);
      setSelIdx(Math.min(idx + 1, lines.length - 1));
    } else {
      const total = p.pieces.length;
      toast(`${kind === "syllable" ? "Syllable" : "Word"} ${p.done}/${total}`);
    }
  };

  const stampForMode = () => {
    if (mode === "line") stampLine();
    else if (mode === "word") stampPieces("word");
    else stampPieces("syllable");
  };

  const shiftAll = (delta: number) => {
    commit(lines.map((l) => {
      const time = Math.max(0, l.time + delta);
      const out: LrcLine = { ...l, time };
      if (out.words?.length) out.words = out.words.map((w) => ({ ...w, time: Math.max(0, w.time + delta) }));
      return out;
    }));
  };

  const nudgeLine = (delta: number) => {
    const idx = Math.min(Math.max(selIdx, 0), lines.length - 1);
    commit(lines.map((l, j) => (j === idx ? { ...l, time: Math.max(0, l.time + delta) } : l)));
  };

  const updateLine = (i: number, patch: Partial<LrcLine>) => {
    const next = lines.map((l, j) => (j === i ? { ...l, ...patch } : l));
    // Text edits invalidate half-stamped runs for that line.
    if (pendingRef.current?.idx === i && "text" in patch) pendingRef.current = null;
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
    commit(lines.filter((_, j) => j !== i));
    setSelIdx((s) => Math.max(0, Math.min(s, lines.length - 2)));
  };

  const useDraft = () => {
    const parsed = toLines(draft);
    if (!parsed.length) {
      toast("Nothing to use yet");
      return;
    }
    commit(parsed);
    setDraft("");
    setSelIdx(0);
  };

  // ---- tools ---------------------------------------------------------------
  const textOut = () => serializeLrc(lines, dec);
  const aiBusy = busy !== null;

  const runOfflineSync = async () => {
    if (aiBusy) return;
    setBusy("offline");
    try {
      const res = await api.lyricsAi("wordsync", textOut());
      const parsed = parseLrc(res.result);
      if (parsed.length) commit(parsed);
      toast("Distributed timings (offline, length-weighted)");
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(null);
    }
  };

  const romanize = async () => {
    if (busy) return;
    setBusy("xlit");
    try {
      const texts = lines.map((l) => l.text);
      const res = await api.lyricsAiLines("transliterate", texts);
      if (res.skipped || !res.lines.length) {
        toast(res.skipped === "script" ? "Lyrics are already Latin script — no romanization needed" : "Nothing to romanize");
        setXlitLines([]);
        return;
      }
      setXlitLines(res.lines);
      toast("Romanized — review below, then store in tags");
    } catch (e) {
      toast(`Romanization failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
    }
  };

  const storeXlit = async () => {
    if (busy) return;
    setBusy("xlit-store");
    try {
      const res = await api.lyricsXlitStore(path);
      if (res.skipped) {
        toast(res.skipped === "script" ? "Already Latin script — nothing stored" : "Romanization came back identical — nothing stored");
      } else {
        toast("Romanization stored (TRANSLITERATION tag — LYRICS keeps the original language)");
        setXlitLines(null);
      }
    } catch (e) {
      toast(`Store failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    const text = textOut();
    if (!text.trim()) {
      toast("Nothing to save");
      return;
    }
    try {
      if (saveTarget === "sidecar" || saveTarget === "both") await api.lyricsWrite(path, text);
      if (saveTarget === "embedded" || saveTarget === "both") await api.lyricsEmbed(path, text);
      toast(`Lyrics saved (${saveTarget === "embedded" ? "tag" : saveTarget === "sidecar" ? ".lrc sidecar" : "tag + .lrc sidecar"})`);
      onSaved?.();
    } catch (e) {
      toast(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  // ---- keybinds -------------------------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
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
      const isTextInput =
        (t instanceof HTMLInputElement && t.dataset.lyrictext !== undefined) ||
        t instanceof HTMLTextAreaElement;
      const isOtherInput =
        (t instanceof HTMLInputElement && t.dataset.lyrictext === undefined) ||
        t instanceof HTMLTextAreaElement ||
        t.isContentEditable;
      if (matchKey(e, keys, "save")) {
        e.preventDefault();
        save();
        return;
      }
      if (isTextInput || isOtherInput) return; // don't hijack typing
      if (matchKey(e, keys, "stampLine")) {
        e.preventDefault();
        stampForMode();
      } else if (matchKey(e, keys, "stampSyllable")) {
        e.preventDefault();
        stampPieces("syllable");
      } else if (matchKey(e, keys, "stampWord")) {
        e.preventDefault();
        stampPieces("word");
      } else if (matchKey(e, keys, "playPause")) {
        e.preventDefault();
        togglePlay();
      } else if (matchKey(e, keys, "seekBack")) {
        e.preventDefault();
        seekTo(playTime - 2);
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
      } else if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, selIdx, lines, playTime, keys, capturing, mode, speed, saveTarget, dec]);

  const sylCount = lines.reduce((n, l) => n + (l.syl ? 1 : 0), 0);
  const wordCount = lines.reduce((n, l) => n + (l.words?.length && !l.syl ? 1 : 0), 0);

  // Portal to <body>: page containers stack above z-70 otherwise (the
  // sidebar would draw over the modal).
  return createPortal(
    <div className="fixed inset-0 z-[70] bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 sm:p-6">
      <div className="rounded-2xl w-full max-w-6xl h-[92vh] flex flex-col shadow-2xl bg-card border border-border overflow-hidden">
        {/* the editor owns a private decoder so stamping never fights the
            main player; playbackRate follows the speed control */}
        <audio
          ref={audioRef}
          onTimeUpdate={(e) => setPlayTime(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => setDur(e.currentTarget.duration || 0)}
          onEnded={() => setPlaying(false)}
          className="hidden"
        />
        {/* header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-white/10 shrink-0">
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-white truncate">
              Lyrics editor <span className="text-zinc-500 font-normal">— {artist ? `${artist} — ` : ""}{track || "untitled"}</span>
            </div>
            <div className="text-[10px] text-zinc-500 mt-0.5">
              {lines.length} lines · {sylCount} syllable-synced · {wordCount} word-synced
              {album ? ` · ${album}` : ""}
            </div>
          </div>
          <div className="relative">
            <button className="btn-ghost !py-1.5 text-xs" onClick={() => setKeysMenu(!keysMenu)} title="Keyboard shortcuts">
              <Keyboard className="h-4 w-4" />
            </button>
            {keysMenu && (
              <>
              <div className="fixed inset-0 z-30" onClick={() => setKeysMenu(false)} />
              <div className="absolute right-0 top-full mt-1 z-40 bg-zinc-950 border border-border rounded-lg shadow-2xl p-1.5 w-80 max-h-[70vh] overflow-auto">
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
              </div>
              </>
            )}
          </div>
          <button className="p-2 rounded-lg hover:bg-white/10 text-zinc-400 hover:text-white" onClick={onClose} title="Close (Esc)">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 min-h-0 flex flex-col lg:flex-row">
          {/* ---- lines ---- */}
          <div className="flex-1 min-w-0 min-h-0 flex flex-col">
            {/* stamp mode + speed */}
            <div className="flex items-center gap-2 px-4 py-2 border-b border-white/10 flex-wrap shrink-0">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500">Stamp</span>
              {(["line", "word", "syllable"] as StampMode[]).map((m) => (
                <button
                  key={m}
                  className={`chip text-[10px] border capitalize ${mode === m ? "bg-accent on-accent border-accent" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
                  onClick={() => { setMode(m); pendingRef.current = null; }}
                  title={m === "line"
                    ? `Space stamps the line's start time (${keys.stampLine})`
                    : m === "word"
                      ? `Space taps word by word (${keys.stampWord})`
                      : `Space taps syllable by syllable (${keys.stampSyllable})`}
                >
                  {m}
                </button>
              ))}
              <span className="w-px h-5 bg-white/10 mx-1" />
              <button className="btn-ghost !px-2 !py-1 text-xs font-mono" onClick={() => stepSpeed(-1)} title={`Slower (${keys.speedSlower})`}>−</button>
              <span className="text-xs font-mono text-zinc-300 min-w-[38px] text-center" title="Playback speed — timestamps always land in song time">{fmtSpeed(speed)}</span>
              <button className="btn-ghost !px-2 !py-1 text-xs font-mono" onClick={() => { setSpeed(1); }} title="Reset speed to 1×">1×</button>
              <button className="btn-ghost !px-2 !py-1 text-xs font-mono" onClick={() => stepSpeed(1)} title={`Faster (${keys.speedFaster})`}>+</button>
              <span className="w-px h-5 bg-white/10 mx-1" />
              <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => shiftAll(-0.1)} title="Shift ALL timestamps 0.1s earlier">−0.1s all</button>
              <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => shiftAll(0.1)} title="Shift ALL timestamps 0.1s later">+0.1s all</button>
              <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => nudgeLine(-0.05)} title="Nudge the selected line 0.05s earlier">sel −0.05</button>
              <button className="btn-ghost !px-1.5 !py-0.5 text-[10px]" onClick={() => nudgeLine(0.05)} title="Nudge the selected line 0.05s later">sel +0.05</button>
              <div className="flex-1" />
              <button className="btn-ghost !py-1 text-xs" onClick={undo} title={`Undo (${keys.undo})`}>
                <Undo2 className="h-3.5 w-3.5" />
              </button>
            </div>

            {lines.length === 0 ? (
              <div className="flex-1 flex flex-col items-center justify-center gap-3 p-6 text-center">
                <div className="text-sm text-zinc-400">No lyrics yet — paste plain or LRC lyrics to start.</div>
                <textarea
                  className="input font-mono text-xs w-full max-w-xl min-h-[180px]"
                  placeholder={"Paste lyrics here, one line per line…\n(or full LRC with [mm:ss.xx] timestamps)"}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                />
                <button className="btn-primary !py-1.5 text-xs" onClick={useDraft} disabled={!draft.trim()}>
                  Use these lyrics
                </button>
              </div>
            ) : (
              <div ref={listRef} className="flex-1 min-h-0 overflow-auto px-4 py-2 space-y-1">
                {lines.map((l, i) => {
                  const isSel = i === selIdx;
                  const isActive = i === activeLine && playing;
                  const pieces = isSel && mode === "syllable" && !l.words ? syllabifyLine(l.text) : null;
                  const pending = pendingRef.current && pendingRef.current.idx === i ? pendingRef.current : null;
                  return (
                    <div
                      key={i}
                      ref={(el) => { rowRefs.current[i] = el; }}
                      className={`group rounded-lg border px-2 py-1.5 transition-colors ${
                        isActive
                          ? "border-accent/60 bg-accent/15"
                          : isSel
                            ? "border-accent/40 bg-white/[0.04]"
                            : "border-transparent hover:border-white/10 hover:bg-white/[0.02]"
                      }`}
                      onClick={() => setSelIdx(i)}
                    >
                      <div className="flex items-center gap-2">
                        <button
                          className="p-0.5 text-zinc-600 hover:text-accent-soft shrink-0"
                          title={`Play from ${fmtDur(l.time)}`}
                          onClick={(e) => { e.stopPropagation(); seekTo(l.time); }}
                        >
                          <Play className="h-3 w-3" />
                        </button>
                        <input
                          data-lyrictime
                          className="w-[58px] text-right font-mono text-[11px] text-zinc-400 outline-none border border-transparent focus:border-accent rounded px-1 py-0.5 shrink-0 tabular-nums"
                          value={l.ts && l.ts !== "[00:00.00]" ? l.ts.slice(1, -1) : fmtDur(l.time)}
                          placeholder="0:00.00"
                          title="Line start — type m:ss.xx"
                          onChange={(e) => {
                            const m = e.target.value.match(/^(?:(\d+):)?(\d{1,2})(?:[.:](\d{1,2}))?$/);
                            if (!m) return;
                            const t = (m[1] ? parseInt(m[1], 10) * 60 : 0) + parseInt(m[2], 10) + (m[3] ? parseInt(m[3].padEnd(2, "0").slice(0, 2), 10) / 100 : 0);
                            updateLine(i, { time: t });
                          }}
                        />
                        <input
                          data-lyrictext
                          className="flex-1 bg-transparent text-sm text-zinc-200 outline-none border border-transparent focus:border-accent rounded px-1 py-0.5 min-w-0"
                          value={l.text}
                          placeholder="Lyric line…"
                          onChange={(e) => updateLine(i, { text: e.target.value })}
                        />
                        {l.syl ? (
                          <span className="chip text-[9px] bg-accent/10 border border-accent/25 text-accent-soft shrink-0" title="Syllable-synced (glued ELRC tags)">
                            {l.words?.length}s
                          </span>
                        ) : l.words?.length ? (
                          <span className="chip text-[9px] bg-white/5 border border-white/15 text-zinc-400 shrink-0" title="Word-synced (ELRC)">
                            {l.words.length}w
                          </span>
                        ) : null}
                        <div className="opacity-0 group-hover:opacity-100 flex gap-1 transition-opacity shrink-0">
                          {l.words?.length ? (
                            <button className="text-zinc-500 hover:text-amber-300" onClick={(e) => { e.stopPropagation(); updateLine(i, { words: undefined }); }} title="Clear this line's word/syllable timings">
                              <Eraser className="h-3.5 w-3.5" />
                            </button>
                          ) : null}
                          <button className="text-zinc-500 hover:text-accent-soft" onClick={(e) => { e.stopPropagation(); addLine(i); }} title="Add line after">
                            <Plus className="h-3.5 w-3.5" />
                          </button>
                          <button className="text-zinc-500 hover:text-red-400" onClick={(e) => { e.stopPropagation(); removeLine(i); }} title="Remove line">
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </div>
                      {isActive && l.words?.length && (
                        <div className="text-sm px-1 mt-1">
                          <KaraokeWords
                            words={l.words}
                            time={playTime}
                            currentClass="text-accent font-semibold scale-110"
                            sungClass="text-white"
                            upcomingClass="text-zinc-500"
                          />
                        </div>
                      )}
                      {isSel && l.words?.length ? (
                        // stamped syllable chips: click one to seek to it
                        <div className="flex flex-wrap gap-1 px-1 mt-1" title="Stamped syllables — click to seek">
                          {l.words!.map((w, wi) => (
                            <button
                              key={wi}
                              className={`chip text-[9px] border ${w.time <= playTime ? "bg-accent/10 border-accent/25 text-accent-soft" : "bg-white/5 border-white/10 text-zinc-400"} hover:border-accent`}
                              onClick={(e) => { e.stopPropagation(); seekTo(w.time); }}
                              title={`Seek to ${fmtDur(w.time)}`}
                            >
                              {w.text.trim() || "·"}
                            </button>
                          ))}
                        </div>
                      ) : null}
                      {pieces && pieces.length > 0 && (
                        <div className="flex flex-wrap gap-1 px-1 mt-1 items-center" title="Syllable chips — each stamp assigns the next one">
                          {pieces.map((s, si) => {
                            const doneCount = pending && pending.idx === i ? pending.done : 0;
                            const isNext = si === doneCount;
                            const isDone = si < doneCount;
                            return (
                              <span
                                key={si}
                                className={`chip text-[9px] border ${
                                  isNext
                                    ? "bg-accent/20 border-accent text-white"
                                    : isDone
                                      ? "bg-accent/5 border-accent/30 text-zinc-300"
                                      : "bg-white/5 border-white/10 text-zinc-500"
                                }`}
                              >
                                {s.text.trim() || "·"}
                              </span>
                            );
                          })}
                          {pending && pending.done > 0 && (
                            <span className="text-[9px] text-zinc-500 ml-1">{pending.done}/{pieces.length}</span>
                          )}
                        </div>
                      )}
                      {xlitLines?.[i]?.trim() && (
                        <div className="text-xs text-zinc-400 px-1 mt-0.5 italic" title="Romanization preview">{xlitLines[i]}</div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* transport */}
            <div className="flex items-center gap-2 px-4 py-2.5 border-t border-white/10 shrink-0">
              <button className={`p-2 rounded-lg ${playing ? "bg-accent on-accent" : "bg-white/10 hover:bg-white/20 text-white"}`} onClick={togglePlay} title={`Play / pause (${keys.playPause})`}>
                {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
              </button>
              <span className="text-[10px] font-mono text-zinc-500 w-10 text-right tabular-nums">{fmtDur(playTime)}</span>
              <input
                type="range"
                min={0}
                max={dur || 0}
                step={0.05}
                value={Math.min(playTime, dur || 0)}
                onChange={(e) => seekTo(Number(e.target.value))}
                className="flex-1 min-w-0"
                title="Seek within the track"
              />
              <span className="text-[10px] font-mono text-zinc-500 w-10 tabular-nums">{fmtDur(dur || 0)}</span>
            </div>
          </div>

          {/* ---- right rail ---- */}
          <div className="lg:w-80 shrink-0 border-t lg:border-t-0 lg:border-l border-white/10 flex flex-col min-h-0 overflow-auto">
            <div className="p-4 space-y-4">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 pb-1.5">Tools</div>
                <div className="space-y-1.5">
                  <button className="btn-ghost !py-1.5 text-xs w-full justify-start flex items-center gap-2" onClick={runOfflineSync} disabled={aiBusy} title="Distribute word/syllable times inside each line's slot, weighted by length — a quick first pass to refine by hand">
                    {busy === "offline" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
                    Auto-distribute timings
                  </button>
                </div>
              </div>

              <div>
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 pb-1.5">Romanization (foreign scripts)</div>
                <div className="flex gap-1.5">
                  <button className="btn-ghost !py-1.5 text-xs flex-1 flex items-center gap-2 justify-center" onClick={romanize} disabled={!!busy || !lines.length} title="Romanize every line (e.g. Japanese → romaji) for review">
                    {busy === "xlit" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Languages className="h-3.5 w-3.5" />}
                    Romanize
                  </button>
                  <button className="btn-ghost !py-1.5 text-xs flex-1 flex items-center gap-2 justify-center" onClick={storeXlit} disabled={!!busy} title="Store the romanization in TRANSLITERATION-<lang>-LATN (+ .romaji.lrc sidecar per settings). The LYRICS field keeps the original language.">
                    {busy === "xlit-store" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                    Store in tags
                  </button>
                </div>
                <div className="text-[10px] text-zinc-600 mt-1.5">
                  The LYRICS tag always keeps the original script — romanization is stored beside it, so Japanese songs stay tagged in Japanese.
                </div>
              </div>

              <div className="pt-1 border-t border-white/10">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 pb-1.5 pt-3">Save</div>
                <div className="flex gap-1.5">
                  <select
                    className="input !py-1.5 !px-2 text-xs flex-1"
                    value={saveTarget}
                    title="Where Save writes lyrics"
                    onChange={(e) => {
                      const v = e.target.value as "embedded" | "sidecar" | "both";
                      setSaveTarget(v);
                      localStorage.setItem("mlo.lyricsSaveTarget", v);
                    }}
                  >
                    <option value="embedded">LYRICS tag</option>
                    <option value="sidecar">.lrc sidecar</option>
                    <option value="both">tag + .lrc</option>
                  </select>
                  <select
                    className="input !py-1.5 !px-2 text-xs w-auto"
                    value={dec}
                    title="Timestamp precision"
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      setDec(v);
                      localStorage.setItem("mlo.lyricsDecimals", String(v));
                    }}
                  >
                    <option value={2}>2 dec</option>
                    <option value={3}>3 dec</option>
                  </select>
                </div>
                <button className="btn-primary !py-1.5 text-xs w-full mt-1.5 flex items-center gap-2 justify-center" onClick={save} title={`Save (${keys.save})`}>
                  <Save className="h-3.5 w-3.5" /> Save lyrics ({keys.save})
                </button>
              </div>

              <div className="pt-1 border-t border-white/10 text-[10px] text-zinc-600 leading-relaxed">
                <div className="uppercase tracking-wider text-zinc-500 pb-1">How to sync</div>
                Press <b>Play</b>, pick the <b>syllable</b> stamp mode, then tap{" "}
                <kbd className="chip bg-raise border border-border px-1">{keys.stampLine}</kbd> (or{" "}
                <kbd className="chip bg-raise border border-border px-1">{keys.stampSyllable}</kbd>) once per{" "}
                syllable as it is sung — the line fills with syllable times and advances.
                Line mode stamps whole lines; the AI button listens to the audio and does
                the whole track for you. Slow the playback to 0.5× for fast passages —
                timestamps always land in song time.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body
  );
}
