import { useEffect, useMemo, useRef, useState } from "react";
import { AudioLines, X } from "lucide-react";
import { api } from "../api";
import { parsePlayerLrc, activeLineRange, KaraokeWords, type LrcLine } from "./LyricsViewer";
import { useLyricsFollow, LYRICS_PAD_BOTTOM, LYRICS_PAD_TOP } from "../lib/lyrScroll";
import Visualizer from "./Visualizer";

// Shared with the fullscreen player: toggling the visualizer from either
// surface keeps the same preference.
const VIZ_KEY = "mlo.np.viz";

/** A right-docked lyrics panel for the player bar's lyrics button: the
 * current track's lyrics with the same synced-line treatment as the
 * fullscreen player (active line highlighted, click a line to seek,
 * stored translations/transliterations as sub-lines) in a compact,
 * display-only pane. AI generation stays the fullscreen player's job —
 * this view shows whatever is stored.
 *
 * Lives above the player bar (bottom anchored) so transport controls
 * stay visible while lyrics are open. */
export default function LyricsSidebar({
  current,
  playing,
  time,
  onSeek,
  getAudioTime,
  onClose,
}: {
  /** Null when nothing is playing — the panel still opens, showing a hint. */
  current: { path: string; title?: string; file: string; albumPath: string; artist?: string; album?: string } | null;
  playing: boolean;
  time: number;
  onSeek: (t: number) => void;
  getAudioTime: () => number;
  onClose: () => void;
}) {
  const [payload, setPayload] = useState<{
    lyrics: string | null;
    xlit: string[] | null;
    trans: string[] | null;
    instrumental: boolean;
    title?: string;
    album?: string;
  } | null>(null);
  const [viz, setViz] = useState(() => localStorage.getItem(VIZ_KEY) !== "0");
  const toggleViz = () => {
    const v = !viz;
    setViz(v);
    localStorage.setItem(VIZ_KEY, v ? "1" : "0");
  };

  const path = current?.path ?? null;
  useEffect(() => {
    if (!path) {
      setPayload(null);
      return;
    }
    let dead = false;
    setPayload(null);
    api
      .tags(path)
      .then((t) => {
        if (dead) return;
        const splitStored = (s: string): string[] =>
          /\[\d{1,2}:\d{1,2}/.test(s)
            ? parsePlayerLrc(s).map((l) => l.text)
            : s.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
        setPayload({
          lyrics: typeof t.lyrics === "string" ? t.lyrics : null,
          xlit: typeof t.lyrics_xlit === "string" && t.lyrics_xlit.trim() ? splitStored(t.lyrics_xlit) : null,
          trans: typeof t.lyrics_trans === "string" && t.lyrics_trans.trim() ? splitStored(t.lyrics_trans) : null,
          instrumental: ((t.tags as Record<string, string>)?.INSTRUMENTAL ?? "").toString().trim() === "1",
          title: (t.tags as Record<string, string>)?.TITLE,
          album: (t.tags as Record<string, string>)?.ALBUM,
        });
      })
      .catch(() => {
        if (!dead) setPayload({ lyrics: null, xlit: null, trans: null, instrumental: false });
      });
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  const instrumental = payload?.instrumental ?? false;
  const lines: LrcLine[] = useMemo(
    () => (payload?.lyrics && !instrumental ? parsePlayerLrc(payload.lyrics) : []),
    [payload?.lyrics, instrumental]
  );
  const synced = lines.length > 0;
  const displayLines: LrcLine[] = useMemo(
    () =>
      instrumental
        ? []
        : synced
          ? lines
          : (payload?.lyrics ?? "").split(/\r?\n/).map((text) => ({ ts: "", time: 0, text })).filter((l) => l.text.trim()),
    [instrumental, synced, lines, payload?.lyrics]
  );

  // ~60 fps lyric clock: shared <audio> element read directly, so line
  // highlights stay as tight as the fullscreen player's. A backgrounded
  // pane pauses rAF — fall back to the event-driven `time` prop when the
  // ticks go stale so following never freezes.
  const [smoothTime, setSmoothTime] = useState(0);
  const smoothTickRef = useRef(0);
  // Word / syllable sweeps are the only 60 fps consumer; line changes ride a
  // 20 Hz tick instead of re-rendering the whole panel every frame.
  const sweepRef = useRef(false);
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const tick = () => {
      const t = getAudioTime();
      if (typeof t === "number" && isFinite(t) && t >= 0) {
        setSmoothTime(sweepRef.current ? t : Math.round(t * 20) / 20);
        smoothTickRef.current = performance.now();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, getAudioTime]);
  const smoothFresh = performance.now() - smoothTickRef.current < 250;
  const dispTime = playing && smoothTime > 0 && smoothFresh ? smoothTime : time;

  // A RANGE, not a single line: same-time lines (duets / backing vocals)
  // highlight together. activeStart is the scroll anchor.
  const { activeStart, activeEnd } = useMemo(() => {
    const [s, e] = activeLineRange(lines, dispTime);
    return { activeStart: s, activeEnd: e };
  }, [lines, dispTime]);

  const scrollRef = useRef<HTMLDivElement>(null);
  // Center the PRIMARY text line, not the block — sub-lines (translation /
  // transliteration) under it must not push the sung line off the middle.
  const primaryRefs = useRef<Record<number, HTMLDivElement | null>>({});

  // Auto-follow owns the pane — the shared controller, so this panel and the
  // fullscreen player behave identically. Only a wheel / touch hands it to
  // the reader, and only for a few seconds.
  const { centerLine, takeOver } = useLyricsFollow({
    active: activeStart,
    time: dispTime,
    playing,
    scroll: scrollRef,
    rows: primaryRefs,
    reset: path,
  });

  // A word / syllable sweep on screen is the one consumer that needs the
  // clock at full rate; line changes ride the 20 Hz tick.
  const sweeping = synced && !!lines[activeStart]?.words?.length;
  useEffect(() => {
    sweepRef.current = sweeping;
  }, [sweeping]);

  const title = payload?.title || current?.title || (current ? current.file.replace(/\.[^.]+$/, "") : "Lyrics");
  const album = payload?.album || current?.album || "";

  return (
    <aside className="fixed top-12 bottom-[5.75rem] right-0 w-full sm:w-[380px] z-30 bg-panel/95 backdrop-blur border-l border-border shadow-2xl flex flex-col">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border/60">
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold truncate">{title}</div>
          {album && <div className="text-[10px] text-zinc-500 truncate">{album}</div>}
        </div>
        <button
          className={`p-1.5 rounded-lg transition-colors ${viz ? "text-accent hover:text-accent-soft" : "text-zinc-500 hover:text-white"} hover:bg-raise`}
          onClick={toggleViz}
          title="Toggle visualizer"
        >
          <AudioLines className="h-4 w-4" />
        </button>
        <button
          className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors"
          onClick={onClose}
          title="Close lyrics"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div
        ref={scrollRef}
        className="relative flex-1 min-h-0 overflow-y-auto overscroll-contain px-5 py-4 no-scrollbar"
        onWheel={(e) => {
          if (e.deltaY !== 0) takeOver();
        }}
        onTouchStart={takeOver}
      >
        {/* Symmetric pads so the first and last line can both reach the
            anchor line — a fixed tail spacer left the pane with no travel
            left at the end of the song. */}
        {displayLines.length > 0 ? (
          <>
            <div style={{ height: LYRICS_PAD_TOP }} />
            {displayLines.map((l, i) => {
            const isActive = synced && activeEnd >= activeStart && i >= activeStart && i <= activeEnd;
            const trans = payload?.trans?.[i];
            const xlit = payload?.xlit?.[i];
            // a stored sub-line that just mirrors the primary line (same
            // words, ignoring case/punctuation) is noise, not a transform
            const essence = (s: string) => s.toLowerCase().replace(/[\W_]+/g, "");
            const dup = (s?: string) => !!s && essence(s) === essence(l.text) && !!essence(l.text);
            return (
              <div
                key={i}
                className={`py-1.5 ${synced ? "cursor-pointer" : ""} ${isActive ? "opacity-100" : synced ? "opacity-70" : ""}`}
                onClick={
                  synced
                    ? () => {
                        // Seek, then center the clicked line directly — the
                        // active-line step misses clicks inside the line
                        // that is already playing.
                        onSeek(l.time);
                        centerLine(i);
                      }
                    : undefined
                }
                title={synced ? "Click to seek" : undefined}
              >
                <div
                  ref={(el) => {
                    primaryRefs.current[i] = el;
                  }}
                  className={`text-[15px] leading-snug font-semibold transition-[transform,color] duration-300 ${
                    isActive ? "text-white" : synced ? "text-zinc-500" : "text-zinc-300"
                  }`}
                  style={{
                    transform: `scale(${isActive ? 1 : 0.92})`,
                    transformOrigin: "0 50%",
                  }}
                >
                  {synced && isActive && l.words?.length ? (
                    <KaraokeWords words={l.words} time={dispTime} />
                  ) : (
                    l.text
                  )}
                </div>
                {xlit && xlit.trim() && !dup(xlit) && xlit.trim() !== l.text.trim() && (
                  <div className="text-xs text-zinc-400 mt-0.5 leading-snug">{xlit}</div>
                )}
                {trans && trans.trim() && !dup(trans) && (
                  <div className="text-xs text-accent-soft/70 mt-0.5 leading-snug">{trans}</div>
                )}
              </div>
            );
            })}
            <div style={{ height: LYRICS_PAD_BOTTOM }} />
          </>
        ) : (
          <div className="h-full flex items-center justify-center text-center px-6">
            {payload === null ? (
              <span className="text-xs text-zinc-600">
                {!current
                  ? "Nothing playing — play an album, artist or playlist and its lyrics appear here."
                  : "Loading lyrics…"}
              </span>
            ) : instrumental ? (
              <span className="text-xs text-zinc-600">
                Instrumental track — stored lyrics stay hidden, as in the fullscreen player.
              </span>
            ) : (
              <span className="text-xs text-zinc-600">
                No lyrics stored for this track — fetch or generate them from the track or album page.
              </span>
            )}
          </div>
        )}
      </div>
      {viz && (
        <div className="border-t border-border/60 px-4 py-2">
          <Visualizer playing={playing} className="h-10 w-full" />
        </div>
      )}
    </aside>
  );
}
