import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Eraser, Keyboard, Loader2, Plus, Save,
  Trash2, Undo2, Wand2,
} from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import {
  parseLrc, serializeLrc, fmtStamp, type LrcLine,
} from "./LyricsViewer";
import {
  loadLyricsKeys, saveLyricsKeys, resetLyricsKeys,
  keyLabel, matchKey, LYRICS_ACTIONS, LYRICS_KEY_DEFAULTS,
  type LyricsAction,
} from "../lib/lyricsKeys";
import { syllabifyLine } from "../lib/syllables";
import Modal from "./Modal";
import Popover from "./Popover";

/** Parse stored lyrics into editor lines; plain text becomes untimed
 * lines ready for stamping. An untimed line carries NO stamp text: its row
 * shows the line's own time through the shared formatter (see `fmtStamp`), at
 * whatever precision the editor is set to, instead of a hard-coded two-decimal
 * placeholder that disagrees with the 3-decimal setting. */
function toLines(text: string): LrcLine[] {
  const trimmed = (text ?? "").trim();
  if (!trimmed) return [];
  const parsed = parseLrc(trimmed);
  if (parsed.length) return parsed;
  return trimmed.split(/\r?\n/).map((ln) => ln.trim()).filter(Boolean)
    .map((text) => ({ ts: "", time: 0, text }));
}

/** Enhanced lyric editor / creator: type or paste lyrics, enter each line's
 *  start time by hand, split a line into words/syllables, auto-distribute
 *  word/syllable times inside stamped lines, and save into the LYRICS tag /
 *  .lrc sidecar. */
export default function LyricsEditorModal({
  path,
  artist,
  track,
  album,
  initialLyrics,
  onClose,
  onSaved,
}: {
  path: string;
  artist?: string;
  track?: string;
  album?: string;
  initialLyrics: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const [lines, setLines] = useState<LrcLine[]>(() => toLines(initialLyrics));
  const [draft, setDraft] = useState("");
  const [selIdx, setSelIdx] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [keysMenu, setKeysMenu] = useState(false);
  const [capturing, setCapturing] = useState<LyricsAction | null>(null);
  const [keys, setKeys] = useState(() => loadLyricsKeys());
  const [saveTarget, setSaveTarget] = useState<"embedded" | "sidecar" | "both">(
    () => (localStorage.getItem("mlo.lyricsSaveTarget") as "embedded" | "sidecar" | "both") ?? "embedded"
  );
  const [dec, setDec] = useState<number>(() => {
    const v = Number(localStorage.getItem("mlo.lyricsDecimals"));
    return v === 2 || v === 3 ? v : 2;
  });
  const historyRef = useRef<LrcLine[][]>([]);
  const capturingRef = useRef<LyricsAction | null>(null);
  const rowRefs = useRef<Record<number, HTMLDivElement | null>>({});

  useEffect(() => {
    setLines(toLines(initialLyrics));
    historyRef.current = [];
  }, [initialLyrics]);

  // Keep the selected row visible while navigating.
  useEffect(() => {
    rowRefs.current[selIdx]?.scrollIntoView({ block: "nearest" });
  }, [selIdx]);

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
  };

  const shiftAll = (delta: number) => {
    // A shifted line's `ts` — text typed before the shift — is stale by the
    // very delta just applied, so it is dropped: the row then shows the
    // formatter's own digits for the new time (the same ones a save writes).
    commit(lines.map((l) => {
      const time = Math.max(0, l.time + delta);
      const out: LrcLine = { ...l, time, ts: "" };
      if (out.words?.length) out.words = out.words.map((w) => ({ ...w, time: Math.max(0, w.time + delta) }));
      return out;
    }));
  };

  const nudgeLine = (delta: number) => {
    const idx = Math.min(Math.max(selIdx, 0), lines.length - 1);
    commit(lines.map((l, j) => (j === idx ? { ...l, time: Math.max(0, l.time + delta), ts: "" } : l)));
  };

  const updateLine = (i: number, patch: Partial<LrcLine>) => {
    const next = lines.map((l, j) => (j === i ? { ...l, ...patch } : l));
    commit(next);
  };

  const addLine = (i: number) => {
    const base = lines[i]?.time ?? lines[lines.length - 1]?.time ?? 0;
    const next = [...lines];
    next.splice(i + 1, 0, { ts: "", time: base, text: "" });
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

  const runOfflineSync = async () => {
    if (busy) return;
    setBusy("offline");
    try {
      const res = await api.lyricsWordsync(path, textOut());
      const parsed = parseLrc(res.lrc);
      if (parsed.length) commit(parsed);
      toast("Distributed timings (offline, length-weighted)");
    } catch (e) {
      toast.error(String(e));
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
    // Escape while a hotkey is being rebound cancels THAT, not the editor —
    // the shared Modal owns Escape now, so it has to consult this first.
    capturingRef.current = capturing;
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
  }, [selIdx, lines.length, keys, capturing, saveTarget, dec]);

  /** Backdrop, cross and Escape all land here. Rebinding a hotkey must not
   *  close the editor — the "press key…" capture is cancelled with Escape. */
  const requestClose = () => {
    if (!capturingRef.current) onClose();
  };

  const sylCount = lines.reduce((n, l) => n + (l.syl ? 1 : 0), 0);
  const wordCount = lines.reduce((n, l) => n + (l.words?.length && !l.syl ? 1 : 0), 0);

  // Portal to <body>: page containers stack above z-70 otherwise (the
  // sidebar would draw over the modal).
  return createPortal(
    <Modal
      onClose={requestClose}
      title={
        <>
          Lyrics editor{" "}
          <span className="text-zinc-500 font-normal">
            — {artist ? `${artist} — ` : ""}{track || "untitled"}
          </span>
        </>
      }
      subtitle={`${lines.length} lines · ${sylCount} syllable-synced · ${wordCount} word-synced${album ? ` · ${album}` : ""}`}
      width="max-w-6xl"
      z="z-[70]"
      bodyClass="!px-0 !py-0 flex flex-col"
      headerExtra={
        <div className="relative">
          <button className="btn-ghost !py-1.5 text-xs" onClick={() => setKeysMenu(!keysMenu)} title="Keyboard shortcuts">
            <Keyboard className="h-4 w-4" />
          </button>
          <Popover
            open={keysMenu}
            onClose={() => setKeysMenu(false)}
            panelClass="w-80 p-1.5 max-h-[70vh] overflow-auto"
          >
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
      }
    >
      <div className="flex-1 min-h-0 flex flex-col lg:flex-row">
        {/* ---- lines ---- */}
        <div className="flex-1 min-w-0 min-h-0 flex flex-col">
          {/* timing tools: shift all / nudge the selected line */}
          <div className="flex items-center gap-2 px-4 py-2 border-b border-white/10 flex-wrap shrink-0">
            <span className="text-[10px] uppercase tracking-wider text-zinc-500">Timing</span>
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
            <div className="flex-1 min-h-0 overflow-auto px-4 py-2 space-y-1">
              {lines.map((l, i) => {
                const isSel = i === selIdx;
                // How this line splits into syllables — the units the
                // auto-distribute tool places timings on.
                const pieces = isSel && !l.words ? syllabifyLine(l.text) : null;
                // The field shows the stamp TEXT that was typed (or
                // parsed); an empty one falls back to the line's time, at
                // the SAME precision a save writes (`fmtStamp`), so the
                // decimals are on screen from the first line on.
                const tsText = l.ts ? (l.ts.startsWith("[") ? l.ts.slice(1, -1) : l.ts) : fmtStamp(l.time, dec);
                return (
                  <div
                    key={i}
                    ref={(el) => { rowRefs.current[i] = el; }}
                    className={`group rounded-lg border px-2 py-1.5 transition-colors ${
                      isSel
                        ? "border-accent/40 bg-white/[0.04]"
                        : "border-transparent hover:border-white/10 hover:bg-white/[0.02]"
                    }`}
                    onClick={() => setSelIdx(i)}
                  >
                    <div className="flex items-center gap-2">
                      <input
                        data-lyrictime
                        className="w-[58px] text-right font-mono text-[11px] text-zinc-400 outline-none border border-transparent focus:border-accent rounded px-1 py-0.5 shrink-0 tabular-nums"
                        value={tsText}
                        placeholder="0:00.00"
                        title="Line start — type m:ss.xx"
                        onChange={(e) => {
                          const v = e.target.value;
                          const m = v.match(/^(?:(\d+):)?(\d{1,2})(?:[.:](\d{1,2}))?$/);
                          // Half-typed text stays in the field (and in
                          // `ts`) without touching the line's time.
                          if (!m) {
                            updateLine(i, { ts: v });
                            return;
                          }
                          const t = (m[1] ? parseInt(m[1], 10) * 60 : 0) + parseInt(m[2], 10) + (m[3] ? parseInt(m[3].padEnd(2, "0").slice(0, 2), 10) / 100 : 0);
                          updateLine(i, { ts: v, time: t });
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
                      <div className="opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 flex gap-1 transition-opacity shrink-0">
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
                    {isSel && l.words?.length ? (
                      // stamped word/syllable chips, each wearing its time
                      <div className="flex flex-wrap gap-1 px-1 mt-1" title="Stamped word/syllable timings">
                        {l.words.map((w, wi) => (
                          <span
                            key={wi}
                            className="chip text-[9px] border bg-accent/10 border-accent/25 text-accent-soft"
                            title={fmtStamp(w.time, dec)}
                          >
                            {w.text.trim() || "·"}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    {pieces && pieces.length > 0 && (
                      <div className="flex flex-wrap gap-1 px-1 mt-1 items-center" title="Syllable split of this line — the units auto-distribute places timings on">
                        {pieces.map((s, si) => (
                          <span key={si} className="chip text-[9px] border bg-white/5 border-white/10 text-zinc-500">
                            {s.text.trim() || "·"}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          </div>

        {/* ---- right rail ---- */}
        <div className="lg:w-80 shrink-0 border-t lg:border-t-0 lg:border-l border-white/10 flex flex-col min-h-0 overflow-auto">
          <div className="p-4 space-y-4">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 pb-1.5">Tools</div>
              <div className="space-y-1.5">
                <button className="btn-ghost !py-1.5 text-xs w-full justify-start flex items-center gap-2" onClick={runOfflineSync} disabled={busy !== null} title="Distribute word/syllable times inside each line's slot, weighted by length — a quick first pass to refine by hand">
                  {busy === "offline" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
                  Auto-distribute timings
                </button>
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
              <div className="uppercase tracking-wider text-zinc-500 pb-1">How to edit</div>
              Type or paste the lyrics, then set each line's start time in the field on its left
              (<b>m:ss.xx</b>). Select a line to see how it splits into syllables, and click{" "}
              <b>Auto-distribute timings</b> to spread word/syllable times across each line's slot.
              <kbd className="chip bg-raise border border-border px-1 mx-1">{keys.prevLine}</kbd>/
              <kbd className="chip bg-raise border border-border px-1">{keys.nextLine}</kbd> move between lines.
            </div>
          </div>
        </div>
      </div>
    </Modal>,
    document.body
  );
}
