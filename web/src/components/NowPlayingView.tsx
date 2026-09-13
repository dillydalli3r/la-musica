import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AudioLines, ChevronDown, Heart, ListMusic, ListPlus, Pause, Play, Repeat, Settings2, Shuffle,
  SkipBack, SkipForward, Volume1, Volume2, VolumeX, X,
} from "lucide-react";
import { api } from "../api";
import VolumePct from "./VolumePct";
import { toast, useStore } from "../store";
import { fmtTech, fmtPair, isVideoFile } from "../lib/fmt";
import { AdvisoryMark } from "./Badges";
import CoverImg from "./CoverImg";
import { activeAnalyser } from "../lib/analyser";
import Visualizer from "./Visualizer";
import { parsePlayerLrc, activeLineRange, KaraokeWords, type LrcLine } from "./LyricsViewer";
import type { Playlist } from "../types";
import { createLyricsGlider, type LyricsGlider } from "../lib/lyrScroll";
import { nextSpeed, fmtSpeed } from "../lib/playback";
import { fmtDuration } from "../pages/LibraryPage";

const XLIT_KEY = "mlo.np.xlit";
const TRANS_KEY = "mlo.np.trans";
const SIZE_KEY = "mlo.np.size"; // sm | md | lg
const KARAOKE_KEY = "mlo.np.karaoke"; // "1" = word-level karaoke, "0" = line highlight (default)
const ORBS_KEY = "mlo.np.orbs"; // "1" = animated background
const VIS_KEY = "mlo.np.vis"; // "1" = background pulses with the beat
const VIZ_KEY = "mlo.np.viz"; // "1" = frequency-bar visualizer visible
const ZOOM_KEY = "mlo.np.lyrzoom.v2"; // lyrics zoom multiplier (persisted)

interface Props {
  current: { path: string; file: string; albumPath: string; artist?: string; album?: string; title?: string; coverFile?: string | null; albumCover?: string | null };
  queuePos: string;
  playing: boolean;
  time: number;
  duration: number;
  /** The popout's live <video> element (music videos) — adopted into the
   * fullscreen picture via a DOM move so playback never reloads. */
  videoEl?: React.RefObject<HTMLVideoElement | null>;
  /** Where that element returns to when the fullscreen view closes. */
  videoHome?: React.RefObject<HTMLDivElement | null>;
  shuffle: boolean;
  loop: boolean;
  liked: boolean;
  onTogglePlay: () => void;
  onSeek: (t: number) => void;
  onStep: (d: 1 | -1) => void;
  onToggleShuffle: () => void;
  onToggleLoop: () => void;
  onToggleLike: () => void;
  onClose: () => void;
  getAudioTime?: () => number;
  /** Shared with the player bar — the bar owns the decoders, the fullscreen
   * view mirrors and edits the rate through these. */
  speed: number;
  onSpeedChange: (s: number) => void;
}

function hexToRgbTriplet(hex?: string | null): [number, number, number] | null {
  if (!hex) return null;
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// Size steps are deliberately close together: the active line reads slightly
// larger than the rest, and the translation/transliteration sub-lines sit
// just under the main line instead of shrinking into fine print.
const LYRIC_SIZES = {
  sm: { line: "text-[15px]", active: "text-[17px]", word: "text-[15px]", xlit: "text-[13px]" },
  md: { line: "text-xl", active: "text-[1.5rem]", word: "text-xl", xlit: "text-[17px]" },
  lg: { line: "text-2xl", active: "text-[2.1rem]", word: "text-2xl", xlit: "text-xl" },
} as const;

/** Ease for the active line's growth — slow out, no snap. */
const LINE_EASE = "transition-[transform,color] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]";

/** How much smaller an inactive line renders next to the active one. The
 * layout is ALWAYS the active size — inactive lines shrink via transform
 * scale, which is GPU-composited and never re-wraps text. Animating
 * font-size instead re-flows and re-wraps the line every frame (the janky,
 * jumpy growth this replaced). */
const INACTIVE_SCALE = { sm: 0.88, md: 0.84, lg: 0.8 } as const;

/** Non-current synced lines read greyed-out (a slight blur + dim grey);
 * hovering a line reveals it in full detail. Plain-text lyrics are never
 * styled — only synced lines get the active/inactive treatment. */
const LINE_BLUR = "blur-[2px] opacity-60 hover:blur-none hover:opacity-100 transition-[opacity,filter] duration-300";

export default function NowPlayingView(p: Props) {
  const { vol, setVol } = useStore();
  // Transliteration + translation default ON: script 15 stores the
  // transforms in tags/sidecars, so they render instantly for processed
  // tracks and the AI is only asked for tracks it hasn't seen yet.
  const [showXlit, setShowXlit] = useState(() => localStorage.getItem(XLIT_KEY) !== "0");
  const [showTrans, setShowTrans] = useState(() => localStorage.getItem(TRANS_KEY) !== "0");
  const [lyricSize, setLyricSize] = useState<keyof typeof LYRIC_SIZES>(
    () => (localStorage.getItem(SIZE_KEY) as keyof typeof LYRIC_SIZES) || "md"
  );
  // Extra zoom multiplier on top of the size preset, persisted — the default
  // is 1.5× (the user's preferred reading size).
  const [lyricZoom, setLyricZoom] = useState<number>(
    () => Number(localStorage.getItem(ZOOM_KEY)) || 1.5
  );
  const [karaoke, setKaraoke] = useState(() => localStorage.getItem(KARAOKE_KEY) === "1");
  const [orbs, setOrbs] = useState(() => localStorage.getItem(ORBS_KEY) !== "0");
  const [vis, setVis] = useState(() => localStorage.getItem(VIS_KEY) !== "0");
  // Frequency-bar visualizer (fullscreen + sidebar), default on.
  const [viz, setViz] = useState(() => localStorage.getItem(VIZ_KEY) !== "0");
  const [options, setOptions] = useState(false);
  const [queueOpen, setQueueOpen] = useState(false);
  const [plOpen, setPlOpen] = useState(false);
  const [newPlName, setNewPlName] = useState("");
  const [tags, setTags] = useState<Record<string, string> | null>(null);
  const [tech, setTech] = useState<{ bitrate?: number; sample_rate?: number; bits_per_sample?: number; codec?: string } | null>(null);
  const [lyricsText, setLyricsText] = useState<string | null>(null);
  const [lyricsFor, setLyricsFor] = useState<string | null>(null);
  // Which track `tags`/`tech` belong to. They are deliberately NOT cleared on
  // track change: blanking them collapsed the tech line under the title and
  // everything below jumped vertically on next/previous (the "title shake").
  // Stale values render until the fresh payload lands; freshness is gated
  // where wrong info would matter (title fallback, instrumental, BPM).
  const [tagsFor, setTagsFor] = useState<string | null>(null);
  const [transforms, setTransforms] = useState<Record<string, string[]>>({});
  const inFlight = useRef<Set<string>>(new Set());
  const lineRefs = useRef<Record<number, HTMLDivElement | null>>({});
  // The PRIMARY text line of each block — with translations/romanization the
  // outer block also carries sub-lines, and centering the block would push
  // the sung line off the middle. The scroller centers this element.
  const primaryRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const lyricsScrollRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();

  // AI availability (checked once): when unconfigured, translation /
  // transliteration are never requested so no endless spinner can appear.
  const [aiReady, setAiReady] = useState<boolean | null>(null);
  useEffect(() => {
    let dead = false;
    api
      .config()
      .then((c) => {
        if (!dead) setAiReady(!!String((c as Record<string, unknown>).ai_base_url ?? "").trim());
      })
      .catch(() => {
        if (!dead) setAiReady(false);
      });
    return () => {
      dead = true;
    };
  }, []);

  const { time, duration } = p;
  const { queue, index, setIndex, setQueue, queueRemoveAt, queueMove } = useStore();
  const queueListRef = useRef<HTMLDivElement>(null);
  // drag-reorder state for the queue drawer (absolute queue indexes)
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [overIdx, setOverIdx] = useState<number | null>(null);

  // Keep the playing row visible when the queue drawer opens.
  useEffect(() => {
    if (!queueOpen) return;
    queueListRef.current
      ?.querySelector(`[data-queue-index="${index}"]`)
      ?.scrollIntoView({ block: "center" });
  }, [queueOpen, index]);

  // ~60 fps lyric clock: while playing, rAF reads the shared <audio> element
  // directly so syllable highlighting isn't stepped at timeupdate's ~4 Hz.
  // A backgrounded pane PAUSES rAF, so freshness is tracked — when ticks
  // stop arriving the clock falls back to the event-driven `time` prop
  // (which always advances), keeping auto-scroll alive for every sync type
  // even while the pane is throttled.
  const [smoothTime, setSmoothTime] = useState(0);
  const smoothTickRef = useRef(0);
  useEffect(() => {
    if (!p.playing) return;
    let raf = 0;
    const tick = () => {
      const t = p.getAudioTime?.();
      if (typeof t === "number" && isFinite(t) && t >= 0) {
        setSmoothTime(t);
        smoothTickRef.current = performance.now();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [p.playing, p.getAudioTime]);
  const smoothFresh = performance.now() - smoothTickRef.current < 250;
  const dispTime = p.playing && smoothTime > 0 && smoothFresh ? smoothTime : time;

  // cover file + dominant color for the ambient background — the album
  // payload also supplies the canonical album artist / album name shown
  // under the cover.
  const { data: album } = useQuery({
    queryKey: ["album", p.current.albumPath],
    queryFn: () => api.album(p.current.albumPath),
    staleTime: 5 * 60 * 1000,
  });
  // Per-track sidecar art wins; the album cover is the fallback — the
  // now-playing art must match the track, not just the album.
  const coverFile = p.current.coverFile ?? album?.cover_file ?? p.current.albumCover ?? null;
  const { data: colorData } = useQuery({
    queryKey: ["coverColor", p.current.albumPath],
    queryFn: () => api.coverColor(p.current.albumPath),
    retry: false,
    staleTime: 10 * 60 * 1000,
  });
  const rgb = hexToRgbTriplet(colorData?.color) ?? [113, 113, 122];

  // ---- background ambience (Apple Music-style, layered) --------------------
  // Driven by the ACTUAL audio signal, never a synthetic clock: each frame
  // reads the shared WebAudio analyser's mean spectrum energy and eases it,
  // so the bloom/background swell with the music that's really playing.
  // With no signal (paused, idle, unobservable stream) everything settles
  // into a barely-there breath — no beat pumping against silence.
  //   * the blurred cover slowly breathes in scale,
  //   * a colored bloom ring behind the artwork swells with real energy,
  //   * each color orb waves with its own phase (CSS keyframes), so light
  //     washes around.
  const bgRef = useRef<HTMLDivElement>(null);
  const bloomRef = useRef<HTMLDivElement>(null);
  const orbsRef = useRef<HTMLDivElement>(null);
  const eased = useRef({ energy: 0 });
  useEffect(() => {
    if (!vis) {
      // effect disabled → restore the static ambience
      if (bgRef.current) {
        bgRef.current.style.opacity = "";
        bgRef.current.style.transform = "";
      }
      if (bloomRef.current) bloomRef.current.style.opacity = "";
      if (orbsRef.current) {
        orbsRef.current.style.transform = "";
        orbsRef.current.style.filter = "";
      }
      return;
    }
    let raf = 0;
    let freq: Uint8Array | null = null;
    const t0 = performance.now();
    const tick = () => {
      const t = (performance.now() - t0) / 1000;
      // Real signal energy 0..1 from the shared analyser (same source the
      // visualizer bars draw). Zero when paused or unobservable.
      let energy = 0;
      if (p.playing) {
        try {
          const an = activeAnalyser();
          if (an) {
            if (!freq || freq.length !== an.frequencyBinCount) {
              freq = new Uint8Array(an.frequencyBinCount);
            }
            an.getByteFrequencyData(freq as Uint8Array<ArrayBuffer>);
            let sum = 0;
            for (let i = 0; i < freq.length; i++) sum += freq[i];
            energy = Math.min(1, sum / (freq.length * 255) * 3.2);
          }
        } catch {
          energy = 0;
        }
      }
      // Fast attack / slow release so swells follow transients, not noise.
      const prev = eased.current.energy;
      eased.current.energy = energy > prev ? energy : prev + (energy - prev) * 0.06;
      const env = eased.current.energy;
      // Blurred cover: a very slow breathing zoom plus a touch of the real
      // energy. Opacity never changes — brightness pumping is what read as
      // "flashing" before.
      const breathe = 0.5 + 0.5 * Math.sin(t * 0.21);
      if (bgRef.current) {
        bgRef.current.style.transform = `scale(${(1.08 + 0.06 * breathe + 0.03 * env).toFixed(4)})`;
      }
      // Bloom: the only energy-visible layer, eased so it swells rather
      // than snaps, and capped well below flash territory.
      if (bloomRef.current) {
        bloomRef.current.style.opacity = String(0.14 + 0.16 * env);
        bloomRef.current.style.transform = `scale(${(0.96 + 0.1 * env).toFixed(4)})`;
      }
      // Color field: all motion lives in the CSS keyframes (large travel,
      // 9-16s loops, per-orb hue). The energy only nudges the field's
      // scale — never opacity or brightness, so nothing can flash.
      if (orbsRef.current) {
        orbsRef.current.style.transform = `scale(${(1 + 0.012 * env).toFixed(4)})`;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [vis, p.playing]);

  // Persisted-toggle helper shared by the options menu and inline buttons.
  const persist = (key: string, v: string) => localStorage.setItem(key, v);

  // ---- video adoption (music videos in the queue) --------------------------
  // The player bar's popout <video> owns the sound and IS the only decoder;
  // the fullscreen view doesn't mirror it with a second muted element
  // (double decode, double network stream, drift-sync that stalls on live
  // transcodes) — it ADOPTS the element itself via a plain DOM move, so
  // playback continues seamlessly across the transition. On close the
  // element returns to its popout home.
  const videoPath = isVideoFile(p.current.file || p.current.path) ? p.current.path : null;
  const videoSlotRef = useRef<HTMLDivElement>(null);
  const [videoFailed, setVideoFailed] = useState(false);
  useEffect(() => setVideoFailed(false), [videoPath]);
  useEffect(() => {
    if (!videoPath) return;
    const el = p.videoEl?.current;
    const slot = videoSlotRef.current;
    const home = p.videoHome?.current;
    if (!el || !slot || !home) return;
    const prevClassName = el.className;
    const prevControls = el.controls;
    // object-CONTAIN: the picture always keeps its original aspect ratio —
    // no cropping, no stretching — and contain IS the largest it can be
    // drawn on screen: it scales the frame up until one dimension touches
    // the viewport edge (full height on a wider screen, full width on a
    // taller one). Remaining edges stay black, like every video player.
    el.className = "absolute inset-0 h-full w-full object-contain bg-black";
    el.controls = false;
    slot.appendChild(el);
    const onError = () => setVideoFailed(true);
    el.addEventListener("error", onError);
    return () => {
      el.removeEventListener("error", onError);
      // Return the element before this slot unmounts — but only if React
      // hasn't already detached it (track change remounts the popout's
      // <video> elsewhere; re-homing a detached node would orphan it).
      if (el.parentNode === slot) {
        home.appendChild(el);
        el.className = prevClassName;
        el.controls = prevControls;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoPath, p.videoEl, p.videoHome]);

  // ---- auto-hiding chrome (video mode) ------------------------------------
  // Like every serious video player: any mouse movement / key / touch shows
  // the top bar, the control overlay and the cursor, then ~2.5s of stillness
  // hides them again over the playing picture. Paused or with a popover open
  // the chrome stays put — hidden controls must never be a surprise.
  const [chromeVisible, setChromeVisible] = useState(true);
  const chromeTimer = useRef<number | null>(null);
  useEffect(() => {
    if (!videoPath) {
      setChromeVisible(true);
      return;
    }
    const arm = () => {
      if (chromeTimer.current !== null) window.clearTimeout(chromeTimer.current);
      chromeTimer.current = window.setTimeout(() => {
        if (p.playing && !queueOpen && !options && !plOpen) setChromeVisible(false);
      }, 2500);
    };
    const poke = () => {
      setChromeVisible(true);
      arm();
    };
    arm();
    const evts: (keyof WindowEventMap)[] = ["mousemove", "mousedown", "keydown", "touchstart"];
    evts.forEach((e) => window.addEventListener(e, poke, { passive: true }));
    return () => {
      evts.forEach((e) => window.removeEventListener(e, poke));
      if (chromeTimer.current !== null) window.clearTimeout(chromeTimer.current);
    };
  }, [videoPath, p.playing, queueOpen, options, plOpen]);
  // Pausing always brings the chrome back.
  useEffect(() => {
    if (!p.playing) setChromeVisible(true);
  }, [p.playing]);

  // ---- lyrics for the current track --------------------------------------
  // Stored transforms (script 15 tags / .romaji.lrc / .<lang>.lrc sidecars)
  // arrive with the same payload and are parsed exactly like the original
  // lyrics, so their lines stay 1:1 with `plainLines` without re-alignment.
  // While the next track's payload loads, the PREVIOUS track's lyrics stay
  // rendered (dimmed, no highlight): resetting to empty first is what made
  // the cover jump sizes / flash to the middle on next / previous.
  // lyricsVersion is bumped after an AI word-sync so the fresh timings
  // reload into the pane.
  const [lyricsVersion] = useState(0); // bump target kept for future reload triggers
  useEffect(() => {
    let dead = false;
    setSmoothTime(0);
    inFlight.current.clear();
    api
      .tags(p.current.path)
      .then((t) => {
        if (dead) return;
        setTags(t.tags ?? {});
        setTech((t.tech as typeof tech) ?? null);
        setTagsFor(p.current.path);
        setLyricsText(typeof t.lyrics === "string" ? t.lyrics : null);
        setLyricsFor(p.current.path);
        const seeded: Record<string, string[]> = {};
        const splitStored = (s: string): string[] =>
          /\[\d{1,2}:\d{1,2}/.test(s)
            ? parsePlayerLrc(s).map((l) => l.text)
            : s.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
        if (typeof t.lyrics_xlit === "string" && t.lyrics_xlit.trim())
          seeded.transliterate = splitStored(t.lyrics_xlit);
        if (typeof t.lyrics_trans === "string" && t.lyrics_trans.trim())
          seeded.translate = splitStored(t.lyrics_trans);
        setTransforms(seeded);
      })
      .catch(() => {
        if (!dead) {
          setTags({});
          setTagsFor(p.current.path);
          setLyricsFor(p.current.path);
        }
      });
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.current.path, lyricsVersion]);

  const instrumental = (tags?.INSTRUMENTAL ?? "").toString().trim() === "1";
  // The lyrics on screen belong to `lyricsFor`; until the new track's
  // payload arrives they are stale — kept for layout stability, dimmed,
  // never highlighted and never sent to the AI.
  const staleLyrics = lyricsFor !== p.current.path;
  const tagsStale = tagsFor !== p.current.path;
  const lines: LrcLine[] = useMemo(
    () => (lyricsText && !instrumental ? parsePlayerLrc(lyricsText) : []),
    [lyricsText, instrumental]
  );
  // Layout (cover sizing, pane presence) follows the on-screen lyrics even
  // while stale so next/previous never reflows the whole view; AI work only
  // ever runs on fresh lyrics.
  const layoutHasLyrics = !!lyricsText?.trim() && !instrumental;
  const hasLyrics = layoutHasLyrics && !staleLyrics;
  const plainLines = useMemo(() => {
    if (!hasLyrics) return [];
    if (lines.length) return lines.map((l) => l.text);
    return (lyricsText ?? "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  }, [hasLyrics, lines, lyricsText]);
  // Synced lyrics carry timestamps; unsynced ones are plain text — both must
  // flow through renderLine so transliteration/translation applies to each.
  const synced = lines.length > 0;
  const displayLines: LrcLine[] = useMemo(
    () => (synced ? lines : plainLines.map((text) => ({ ts: "", time: 0, text }))),
    [synced, lines, plainLines]
  );

  // ---- translation / transliteration -------------------------------------
  // aiReady === null means the config probe is still running — hold off so
  // we never fire a request that's destined to fail (and spin forever).
  useEffect(() => {
    if (aiReady === null || !aiReady || staleLyrics) return;
    const modes = [
      ...(showXlit ? ["transliterate"] : []),
      ...(showTrans ? ["translate"] : []),
    ];
    if (!hasLyrics || !plainLines.length) return;
    for (const mode of modes) {
      const key = `${p.current.path}|${mode}`;
      if (inFlight.current.has(key) || transforms[mode]) continue;
      inFlight.current.add(key);
      api
        .lyricsAiLines(mode as "translate" | "transliterate", plainLines)
        .then((r) => setTransforms((prev) => ({ ...prev, [mode]: r.lines })))
        .catch((e) => {
          // AI failed (rate limit, bad key…) — mark done with no output so
          // the "transforming…" indicator never gets stuck on screen, and
          // surface the reason instead of failing silently.
          setTransforms((prev) => (prev[mode] ? prev : { ...prev, [mode]: [] }));
          toast(`${mode === "translate" ? "Translation" : "Transliteration"} failed: ${e instanceof Error ? e.message : e}`);
        })
        .finally(() => inFlight.current.delete(key));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiReady, showXlit, showTrans, hasLyrics, plainLines, p.current.path]);

  // ---- active line ---------------------------------------------------------
  // A RANGE, not a single line: lines stamped at the same moment (duets,
  // backing vocals) form one cluster and highlight together. aStart is the
  // scroll target (first line of the cluster).
  const { aStart, aEnd } = useMemo(() => {
    if (staleLyrics) return { aStart: -1, aEnd: -1 }; // old lyrics vs new clock
    const [s, e] = activeLineRange(lines, dispTime);
    return { aStart: s, aEnd: e };
  }, [lines, dispTime, staleLyrics]);
  const activeLine = aStart;

  // Glider owns the lyrics pane's scrolling (zoom-safe, retargetable) —
  // recreated whenever the pane mounts/unmounts (layout follows lyric
  // presence, and music videos drop the column entirely).
  const gliderRef = useRef<LyricsGlider | null>(null);
  useEffect(() => {
    const c = lyricsScrollRef.current;
    if (!c) return;
    const g = createLyricsGlider(c);
    gliderRef.current = g;
    return () => {
      if (gliderRef.current === g) gliderRef.current = null;
    };
  }, [layoutHasLyrics, videoPath]);

  // Auto-follow owns the pane. Nothing pauses it implicitly — not even the
  // pointer resting on the lyrics (that hover-pause read as "auto-scroll
  // stopped working" whenever the cursor was parked over the pane). Only
  // an explicit wheel / touch takes over, and following picks back up at
  // the next line change, gliding from wherever the pane is.

  // Seek vs glide: a real jump of the song clock (>1.2s between frames)
  // marks a SEEK — the pane snaps to the new position. Everything else
  // (normal line steps, clicking a lyric line) glides. Clicking a line
  // also moves the audio clock, so it sets a short glide window that wins
  // over the seek mark: navigating by lyric line stays animated even at
  // song start — and the click centers the line directly, so it lands
  // correctly even when it doesn't change the active line or while the
  // pointer hovers the pane.
  const seekMarkRef = useRef(0);
  const glideMarkRef = useRef(0);
  const prevDispRef = useRef(-1);
  useEffect(() => {
    const prev = prevDispRef.current;
    prevDispRef.current = dispTime;
    if (prev >= 0 && Math.abs(dispTime - prev) > 1.2) seekMarkRef.current = Date.now();
  }, [dispTime]);

  useEffect(() => {
    if (activeLine < 0) return;
    const el = primaryRefs.current[activeLine] ?? lineRefs.current[activeLine];
    if (!el) return;
    const now = Date.now();
    const animate = now - glideMarkRef.current < 1500 || now - seekMarkRef.current > 600;
    gliderRef.current?.center(el, !animate);
    // Deps: activeLine only — the effect intentionally ignores dispTime so
    // the pane moves one step per line change, not per frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeLine]);

  // New track → rewind the lyrics pane to the top.
  useEffect(() => {
    const c = lyricsScrollRef.current;
    gliderRef.current?.stop();
    if (c) c.scrollTop = 0;
  }, [p.current.path]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Esc steps OUT of the browser fullscreen first (matching native
        // video players); a second Esc then closes the viewer itself.
        if (plOpen) setPlOpen(false);
        else if (document.fullscreenElement) document.exitFullscreen().catch(() => { /* gone */ });
        else p.onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plOpen]);

  const toggleOpt = (which: "xlit" | "trans") => {
    if (which === "xlit") {
      const v = !showXlit;
      setShowXlit(v);
      persist(XLIT_KEY, v ? "1" : "0");
    } else {
      const v = !showTrans;
      setShowTrans(v);
      persist(TRANS_KEY, v ? "1" : "0");
    }
  };

  // ---- add to playlist -----------------------------------------------------
  const { data: playlists } = useQuery({
    queryKey: ["playlists"],
    queryFn: api.playlists,
    enabled: plOpen,
  });
  const addToPlaylist = async (pl: Playlist) => {
    try {
      await api.playlistAdd(pl.id, [p.current.path]);
      toast(`Added "${title}" to ${pl.name}`);
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlist", pl.id] });
    } catch (e) {
      toast(String(e));
    }
  };
  const createPlaylistAndAdd = async () => {
    const name = newPlName.trim();
    if (!name) return;
    try {
      const pl = await api.createPlaylist(name, "manual");
      await api.playlistAdd(pl.id, [p.current.path]);
      toast(`Added "${title}" to ${pl.name}`);
      setNewPlName("");
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlists"] });
    } catch (e) {
      toast(String(e));
    }
  };

  const size = LYRIC_SIZES[lyricSize];
  const transforming =
    !!aiReady && hasLyrics && ((showXlit && !transforms.transliterate) || (showTrans && !transforms.translate));

  const renderLine = (l: LrcLine, i: number) => {
    // Every line of the current same-time cluster (duets / backing vocals)
    // reads as active; the anchor is the cluster's first line.
    const isActive = synced && aEnd >= aStart && i >= aStart && i <= aEnd;
    const xlit = showXlit ? transforms.transliterate?.[i] : undefined;
    const trans = showTrans ? transforms.translate?.[i] : undefined;
    // When transliteration is on, the romanized text IS the primary line —
    // for Latin-script originals it equals the original, so hiding the
    // original loses nothing. Karaoke word timing only fits the original
    // wording, so it applies only when the original is displayed (and only
    // for synced lyrics, which is where word timings exist).
    const replaced = !!xlit && xlit.trim() !== "" && xlit.trim() !== l.text.trim();
    const primary = replaced ? xlit : l.text;
    // A "translation" that repeats the line it sits under (English lyrics
    // "translated" to English) adds nothing — skip those sub-lines.
    const essence = (s: string) => s.toLowerCase().replace(/[\W_]+/g, "");
    const transDup = !!trans && (
      essence(trans) === essence(l.text) ||
      (replaced && essence(trans) === essence(xlit))
    );
    return (
      <div
        key={i}
        ref={(el) => {
          lineRefs.current[i] = el;
        }}
        className={`py-2 ${synced ? "cursor-pointer" : ""} ${
          isActive ? "opacity-100" : synced ? LINE_BLUR : ""
        }`}
        onClick={
          synced
            ? () => {
                glideMarkRef.current = Date.now();
                p.onSeek(l.time);
                // Center the clicked line NOW — the activeLine effect alone
                // misses clicks within the same line and is suppressed
                // while the pointer hovers the pane.
                const el = primaryRefs.current[i];
                if (el) gliderRef.current?.center(el, false);
              }
            : undefined
        }
        title={synced ? "Click to seek" : undefined}
      >
        {/* One layout for every state: the line block is laid out at the
            active size and scaled down when inactive — a compositor-only
            animation, so text never re-wraps mid-growth and the scroll
            target never shifts under it. Weight is constant for the same
            reason (weight changes re-flow glyph widths). */}
        <div
          ref={(el) => {
            primaryRefs.current[i] = el;
          }}
          className={`${size.active} leading-snug ${synced ? `${LINE_EASE} font-semibold` : ""} ${
            isActive ? "text-white" : synced ? "text-zinc-500" : "text-zinc-200"
          }`}
          style={
            synced
              ? { transform: `scale(${isActive ? 1 : INACTIVE_SCALE[lyricSize]})`, transformOrigin: "0 50%" }
              : { transform: `scale(${INACTIVE_SCALE[lyricSize]})`, transformOrigin: "0 50%" }
          }
        >
          {!replaced && isActive && karaoke && l.words?.length ? (
            <KaraokeWords words={l.words} time={dispTime} />
          ) : primary}
          {trans && !transDup && (
            <div className={`${size.xlit} font-normal text-accent-soft/70 mt-0.5 leading-snug`}>{trans}</div>
          )}
        </div>
      </div>
    );
  };

  // Freshness-gated tag fallbacks: while the next track's payload loads,
  // never substitute the PREVIOUS track's tag values into these strings —
  // the queue already knows title/artist/album, so prefer those.
  const freshTags = tagsStale ? undefined : tags;
  const title = p.current.title || freshTags?.TITLE || p.current.file.replace(/\.[^.]+$/, "");
  // Same vertical order and formatting as the player bar's text block:
  // title, then ALBUM, then ARTIST — the two sub-lines render identically.
  const albumLine = p.current.album || freshTags?.ALBUM || album?.meta?.ALBUM || "—";
  const artistLine = p.current.artist || freshTags?.ARTIST || p.current.albumPath.split("/").pop() || "";
  const upNext = !p.shuffle ? queue[index + 1] as
    | { title?: string; artist?: string; file: string }
    | undefined : undefined;
  const upNextLabel = upNext
    ? `${upNext.title || upNext.file.replace(/\.[^.]+$/, "")}${upNext.artist ? ` — ${upNext.artist}` : ""}`
    : "";
  // Ultra-condensed readout under the title: "16/44.1" (same as the player
  // bar); the tooltip carries the full codec/bitrate detail.
  const techStr = fmtPair(tech);
  const techTip = fmtTech(tech);

  const VolIcon = vol <= 0 ? VolumeX : vol < 0.5 ? Volume1 : Volume2;

  // Shared control blocks — the audio layout shows them under the cover;
  // the fullscreen-video layout overlays them at the bottom of the picture.
  const textBlock = (
    /* Every text row keeps a fixed height and is ALWAYS rendered —
       blanking a row while the next track's tags load is what made
       the block (and the title itself) shake on next/previous. */
    <div className="text-center w-full max-w-[26rem] min-w-0">
      <div className="h-8 flex items-center justify-center gap-2" title={title}>
        <div className="text-2xl font-bold text-white truncate">{title}</div>
        <AdvisoryMark value={freshTags?.ITUNESADVISORY} />
        {/* bit depth/sample rate rides beside the title, same as the
            player bar; tooltip carries the full codec/bitrate detail */}
        {techStr && (
          <span className="text-[11px] font-mono text-zinc-500 shrink-0" title={techTip || undefined}>
            {techStr}
          </span>
        )}
      </div>
      <div className="h-5 mt-1 flex items-center justify-center" title={albumLine}>
        <div className="text-sm text-zinc-400 truncate">{albumLine}</div>
      </div>
      <div className="h-5 mt-0.5 flex items-center justify-center" title={artistLine}>
        <div className="text-sm text-zinc-400 truncate">{artistLine}</div>
      </div>
    </div>
  );
  const transportRow = (
    <div className="flex items-center justify-center gap-2.5 flex-wrap">
      <button className={`p-2 rounded-lg hover:bg-white/10 ${p.shuffle ? "text-accent" : "text-zinc-500"}`} onClick={p.onToggleShuffle} title="Shuffle">
        <Shuffle className="h-4 w-4" />
      </button>
      <button className="p-2.5 rounded-lg hover:bg-white/10 text-white" onClick={() => p.onStep(-1)} title="Previous track">
        <SkipBack className="h-5 w-5" />
      </button>
      <button
        className="p-4 rounded-lg bg-accent on-accent hover:bg-accent-soft shadow-lg"
        onClick={p.onTogglePlay}
        title="Play / pause (Space)"
      >
        {p.playing ? <Pause className="h-6 w-6" /> : <Play className="h-6 w-6 ml-0.5" />}
      </button>
      <button className="p-2.5 rounded-lg hover:bg-white/10 text-white" onClick={() => p.onStep(1)} title="Next track">
        <SkipForward className="h-5 w-5" />
      </button>
      <button className={`p-2 rounded-lg hover:bg-white/10 ${p.loop ? "text-accent" : "text-zinc-500"}`} onClick={p.onToggleLoop} title="Repeat one">
        <Repeat className="h-4 w-4" />
      </button>
      <button
        className="p-2 rounded-lg hover:bg-white/10 text-xs font-mono text-zinc-400 min-w-[46px]"
        onClick={() => p.onSpeedChange(nextSpeed(p.speed, 1))}
        title="Playback speed — [ slower · ] faster · 0 reset to 1×"
      >
        {fmtSpeed(p.speed)}
      </button>
      <span className="w-px h-6 bg-white/15 mx-1" />
      <button
        className={`p-2 rounded-lg hover:bg-white/10 ${p.liked ? "text-accent" : "text-zinc-500 hover:text-zinc-300"}`}
        onClick={p.onToggleLike}
        title={p.liked ? "Unlike" : "Like this track"}
      >
        <Heart className={`h-[18px] w-[18px] ${p.liked ? "fill-current" : ""}`} />
      </button>
      <div className="relative">
        <button
          className={`p-2 rounded-lg hover:bg-white/10 ${plOpen ? "text-accent bg-white/10" : "text-zinc-500 hover:text-zinc-300"}`}
          onClick={() => setPlOpen(!plOpen)}
          title="Add this track to a playlist"
        >
          <ListPlus className="h-[18px] w-[18px]" />
        </button>
        {plOpen && (
          <>
            <div className="fixed inset-0 z-10" onClick={() => setPlOpen(false)} />
            <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 z-20 rounded-lg shadow-2xl border border-border p-1.5 w-60 bg-zinc-950 max-h-72 flex flex-col">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Playlists</div>
              <div className="overflow-y-auto min-h-0">
                {(playlists ?? []).filter((pl) => pl.kind === "manual").map((pl) => (
                  <button
                    key={pl.id}
                    className="w-full text-left px-2 py-1.5 rounded-lg text-xs text-zinc-300 hover:bg-white/10 hover:text-white truncate"
                    onClick={() => addToPlaylist(pl)}
                    title={`Add to ${pl.name}`}
                  >
                    {pl.name}
                  </button>
                ))}
                {!(playlists ?? []).some((pl) => pl.kind === "manual") && (
                  <div className="px-2 py-1.5 text-[11px] text-zinc-600">No manual playlists yet</div>
                )}
              </div>
              <div className="flex items-center gap-1.5 pt-1.5 mt-1 border-t border-white/10">
                <input
                  className="input !py-1 !px-2 text-[11px] flex-1 min-w-0"
                  placeholder="New playlist name"
                  value={newPlName}
                  onChange={(e) => setNewPlName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") createPlaylistAndAdd();
                  }}
                  autoFocus
                />
                <button className="btn-primary !py-1 !px-2 text-[11px] shrink-0" onClick={createPlaylistAndAdd} disabled={!newPlName.trim()}>
                  Create
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
  const seekRow = (
    <div className="flex items-center gap-2 text-xs text-zinc-400 w-full max-w-[26rem] px-2">
      <span className="w-10 text-right font-mono tabular-nums">{fmtDuration(dispTime)}</span>
      <input
        type="range"
        min={0}
        max={duration || 0}
        step={0.05}
        value={Math.min(dispTime, duration || 0)}
        onChange={(e) => p.onSeek(Number(e.target.value))}
        className="flex-1 min-w-0 seek-fat"
        title="Seek"
      />
      <span className="w-10 font-mono tabular-nums">{fmtDuration(duration)}</span>
      {/* the divider sits dead-center between the duration and volume
          groups, on the same optical axis as the sliders */}
      <span className="w-px h-5 bg-white/15 self-center shrink-0 mx-2" />
      <div className="hidden md:flex items-center gap-1.5 text-zinc-500 shrink-0" title={`Volume — ${Math.round(vol * 100)}%`}>
        <VolIcon className="h-4 w-4" />
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={vol}
          onChange={(e) => setVol(Number(e.target.value))}
          className="w-24 seek-fat"
          title="Volume"
        />
        <VolumePct value={vol} onChange={setVol} />
      </div>
    </div>
  );

  return (
    <div className={`fixed inset-0 z-50 bg-zinc-950 overflow-clip ${videoPath && !chromeVisible ? "cursor-none" : ""}`}>
      {/* overflow-clip (not hidden): a hidden box is still a scroll container,
          so wheel / scrollIntoView can silently scroll the whole overlay and
          leave the view "stuck" half-rendered. Clip can never be scrolled. */}
      {/* Music videos own the whole screen: the picture fills the viewport
          (object-contain over black), controls overlay the bottom edge. The
          cover/orb ambience is skipped — the video IS the background. */}
      {!videoPath && (
      <>
      {/* ---- ambient background: blurred cover + drifting color orbs,
              swelling with the beat when the pulse effect is on ---- */}
      <div ref={bgRef} className="absolute inset-0 blur-3xl" style={{ opacity: 0.25, transform: "scale(1.1)" }}>
        <CoverImg albumPath={p.current.albumPath} coverFile={coverFile} wrapperClass="w-full h-full" />
      </div>
      {orbs && (
        <div ref={orbsRef} className="absolute inset-0 pointer-events-none">
          {/* .orb-field carries the slow hue-cycle CSS animation; the beat
              pump (JS) stays on the outer container so the two never fight */}
          <div className="orb-field absolute inset-0">
            <div
              className="orb orb-a w-[55vw] h-[55vw] -top-[15vw] -left-[10vw]"
              style={{ background: `radial-gradient(circle at 35% 35%, rgb(${rgb.join(" ")} / 0.9), transparent 65%)` }}
            />
            <div
              className="orb orb-b w-[48vw] h-[48vw] bottom-[-14vw] right-[-8vw]"
              style={{ background: `radial-gradient(circle at 38% 32%, rgb(${rgb.join(" ")} / 0.85), transparent 62%)`, filter: "blur(80px) hue-rotate(55deg)" }}
            />
            <div
              className="orb orb-c w-[38vw] h-[38vw] top-[28%] left-[36%]"
              style={{ background: `radial-gradient(circle at 62% 38%, rgb(${rgb.map((v) => Math.min(255, v + 40)).join(" ")} / 0.8), transparent 60%)`, filter: "blur(80px) hue-rotate(-65deg)" }}
            />
            <div
              className="orb orb-d w-[30vw] h-[30vw] top-[-8vw] right-[12vw]"
              style={{ background: `radial-gradient(circle at 30% 60%, rgb(${rgb.map((v) => Math.max(0, v - 20)).join(" ")} / 0.75), transparent 58%)`, filter: "blur(70px) hue-rotate(150deg)" }}
            />
          </div>
        </div>
      )}
      {/* bloom ring behind the artwork — swells on the beat when the
          ambience effect is on */}
      <div
        ref={bloomRef}
        aria-hidden
        className="absolute inset-0 pointer-events-none"
        style={{
          background: `radial-gradient(ellipse 62% 52% at 50% 55%, rgb(${rgb.join(" ")} / 0.5), transparent 70%)`,
          opacity: 0.15,
        }}
      />
      {/* legibility wash — deliberately light so the animated color field
          stays visible; only the very top and bottom darken for the bars */}
      <div className="absolute inset-0 bg-gradient-to-b from-zinc-950/55 via-zinc-950/20 to-zinc-950/80" />
      </>
      )}

      {videoPath && (
        <div className="absolute inset-0 bg-black">
          {!videoFailed ? (
            <>
              {/* the popout's live <video> is adopted into this slot */}
              <div ref={videoSlotRef} className="absolute inset-0" />
              {/* click surface for play/pause — sits above the video, below
                  the bottom controls, and keeps working while the chrome is
                  hidden */}
              <div
                className="absolute inset-0 z-[1] cursor-pointer"
                onClick={p.onTogglePlay}
                title="Play / pause (Space)"
              />
            </>
          ) : (
            <div className="absolute inset-0 flex items-center justify-center p-8">
              <div className="max-w-lg text-center text-xs text-zinc-400 border border-white/10 rounded-2xl bg-black/60 p-6">
                This video can't be decoded by this browser. Install ffmpeg (Dependencies) to enable automatic
                transcoding, remux it (album page → Remux videos), or open the file externally.
              </div>
            </div>
          )}
          {/* bottom control overlay — same blocks as the audio layout; eases
              away (with the cursor) while the video plays untouched */}
          <div
            className={`absolute inset-x-0 bottom-0 z-10 bg-gradient-to-t from-black/85 via-black/45 to-transparent pt-24 pb-5 px-4 sm:px-8 transition-[opacity,transform] duration-300 ease-out ${
              chromeVisible ? "" : "pointer-events-none opacity-0 translate-y-6"
            }`}
          >
            <div className="max-w-3xl mx-auto flex flex-col items-center gap-4">
              {textBlock}
              {transportRow}
              {seekRow}
            </div>
          </div>
        </div>
      )}

      {/* pointer-events pass through to the fullscreen video; the top bar
          opts back in so its buttons still work. z-[2]: the video layer's
          click-catcher is z-[1] in the same (root) stacking context — the
          bar must paint and hit-test above it, while the bottom controls
          (z-10) and queue drawer (z-20) stay above the bar. */}
      <div className={`relative z-[2] h-full flex flex-col ${videoPath ? "pointer-events-none" : ""}`}>
        {/* top bar — exit button top-left, queue/options cluster top-right;
            eases away with the bottom overlay while the video plays */}
        <div
          className={`flex items-center justify-between px-5 py-3 transition-[opacity,transform] duration-300 ease-out ${
            videoPath ? (chromeVisible ? "pointer-events-auto" : "pointer-events-none opacity-0 -translate-y-3") : ""
          }`}
        >
          <button
            className="p-2 rounded-lg hover:bg-white/10 text-zinc-400 hover:text-white"
            onClick={p.onClose}
            title="Exit fullscreen (Esc)"
          >
            <ChevronDown className="h-5 w-5" />
          </button>
          <div className="flex items-center gap-1 min-w-0">
            {p.queuePos && (
              <span className="text-[10px] font-mono text-zinc-500 mr-1 tabular-nums" title="Queue position">
                {p.queuePos}
              </span>
            )}
            {/* up next — lives beside the queue it describes; click opens it.
                Always on the bar: inert when nothing is queued. */}
            <button
              className={`max-w-[15rem] min-w-0 items-center gap-1.5 px-1.5 py-1 rounded-md text-[10px] font-mono hidden sm:flex ${
                upNextLabel
                  ? "text-zinc-500 hover:text-white hover:bg-white/10"
                  : "text-zinc-600 opacity-40 pointer-events-none"
              }`}
              onClick={() => setQueueOpen(true)}
              title={upNextLabel ? `Up next — ${upNextLabel} · click to view the queue` : "Up next — nothing queued"}
            >
              <span className="uppercase tracking-widest text-zinc-600 shrink-0">Up next</span>
              <span className="truncate">{upNextLabel || "—"}</span>
            </button>
            <button
              className={`p-2 rounded-lg hover:bg-white/10 ${queueOpen ? "text-white bg-white/10" : "text-zinc-400 hover:text-white"}`}
              onClick={() => setQueueOpen(!queueOpen)}
              title="Up next (queue)"
            >
              <ListMusic className="h-5 w-5" />
            </button>
            <button
              className={`p-2 rounded-lg hover:bg-white/10 ${viz ? "text-accent" : "text-zinc-400 hover:text-white"}`}
              onClick={() => {
                const v = !viz;
                setViz(v);
                persist(VIZ_KEY, v ? "1" : "0");
              }}
              title="Toggle visualizer bars"
            >
              <AudioLines className="h-5 w-5" />
            </button>
            <div className="relative">
              <button
                className={`p-2 rounded-lg hover:bg-white/10 ${options ? "text-white bg-white/10" : "text-zinc-400 hover:text-white"}`}
                onClick={() => setOptions(!options)}
                title="Lyrics & display options"
              >
                <Settings2 className="h-5 w-5" />
              </button>
              {options && (
                <>
                  {/* click-away shield so the popover never lingers over the
                      lyrics; the panel itself is opaque and layered above
                      everything so it reads cleanly over moving text */}
                  <div className="fixed inset-0 z-40" onClick={() => setOptions(false)} />
                  <div className="absolute right-0 top-full mt-1 z-50 rounded-lg shadow-2xl p-1.5 w-72 max-w-[calc(100vw-1.5rem)] bg-zinc-950 border border-border">
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Lyrics</div>
                {[
                    { id: "xlit" as const, label: "Transliteration (romanized)", on: showXlit, act: () => toggleOpt("xlit") },
                    { id: "trans" as const, label: "Translation", on: showTrans, act: () => toggleOpt("trans") },
                    {
                      id: "karaoke" as const,
                      label: "Karaoke syllable sweep",
                      on: karaoke,
                      act: () => {
                        const v = !karaoke;
                        setKaraoke(v);
                        persist(KARAOKE_KEY, v ? "1" : "0");
                      },
                    },
                  ].map((o) => (
                    <label key={o.id} className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                      <input type="checkbox" className="accent-[var(--accent)]" checked={o.on} onChange={o.act} />
                      {o.label}
                    </label>
                  ))}
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1">Lyrics size</span>
                    {(["sm", "md", "lg"] as const).map((s) => (
                      <button
                        key={s}
                        className={`chip text-[10px] border ${lyricSize === s ? "bg-accent on-accent border-accent" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
                        onClick={() => {
                          setLyricSize(s);
                          persist(SIZE_KEY, s);
                        }}
                      >
                        {s.toUpperCase()}
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1" title="Zoom the lyrics pane — saved for every future visit">Zoom</span>
                    <input
                      type="range"
                      min={0.85}
                      max={1.6}
                      step={0.05}
                      value={lyricZoom}
                      onChange={(e) => {
                        const v = Number(e.target.value);
                        setLyricZoom(v);
                        persist(ZOOM_KEY, String(v));
                      }}
                      className="w-28"
                    />
                    <span className="w-9 text-right text-[10px] text-zinc-500 tabular-nums">{Math.round(lyricZoom * 100)}%</span>
                  </div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-1 pt-2 pb-1">Background</div>
                  <label className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={orbs}
                      onChange={() => {
                        const v = !orbs;
                        setOrbs(v);
                        persist(ORBS_KEY, v ? "1" : "0");
                      }}
                    />
                    Animated color drift
                  </label>
                  <label className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={vis}
                      onChange={() => {
                        const v = !vis;
                        setVis(v);
                        persist(VIS_KEY, v ? "1" : "0");
                      }}
                    />
                    Background pulse
                  </label>
                  <label className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={viz}
                      onChange={() => {
                        const v = !viz;
                        setViz(v);
                        persist(VIZ_KEY, v ? "1" : "0");
                      }}
                    />
                    Visualizer bars
                  </label>
                  <div className="text-[10px] text-zinc-600 px-2 pt-1">
                    AI translation uses Settings → AI; results are cached per track.
                  </div>
                </div>
                </>
              )}
            </div>
          </div>
        </div>

        {/* main area — music videos never reach this branch: their picture
            fills the screen behind the top bar (see the video layer above)
            with the same controls overlaid at the bottom edge */}
        {!videoPath && (
        <div className={`flex-1 min-h-0 flex flex-col lg:flex-row items-center gap-4 sm:gap-8 px-4 sm:px-8 pb-4 overflow-clip ${layoutHasLyrics ? "" : "lg:justify-center"}`}>
          {/* left column: cover, track/album/artist, all playback controls —
              centered as a group inside the full column height */}
          <div
            className={`flex flex-col items-center justify-center gap-4 shrink-0 min-w-0 ${
              layoutHasLyrics ? "lg:w-[42%] lg:h-full" : ""
            }`}
          >
            <div className="relative">
              {orbs && (
                <div
                  className="artwork-glow absolute -inset-6 rounded-[2rem] blur-2xl"
                  style={{ background: `radial-gradient(circle, rgb(${rgb.join(" ")} / 0.55), transparent 70%)` }}
                />
              )}
              <CoverImg
                albumPath={p.current.albumPath}
                coverFile={coverFile}
                // ONE size with or without lyrics — the art must never
                // jump when a track's lyrics load or finish.
                wrapperClass="relative rounded-2xl shadow-2xl border border-white/10 bg-raise overflow-hidden w-72 h-72 lg:w-[min(28rem,48vh)] lg:h-[min(28rem,48vh)]"
              />
            </div>
            {textBlock}
            {transportRow}
            {/* seek + volume — a single line under the transport */}
            {seekRow}
            {/* frequency-bar visualizer — the same one the fullscreen view
                uses; toggle via the button in the top bar or options menu */}
            {viz && (
              <div className="w-full max-w-[26rem] px-2">
                <Visualizer playing={p.playing} className="h-12 w-full" />
              </div>
            )}
          </div>

          {/* lyrics column — plain, no panel, hugging the right edge; flex-1
              below lg so it can't overflow the viewport (h-full there would
              double-count with the cover block and clip the bottom half
              outside the scroll pane). While the next track's lyrics load,
              the previous ones stay on screen dimmed instead of collapsing
              the layout (which flashed the cover to the middle). */}
          {!videoPath && layoutHasLyrics && (
            <div className="flex-1 min-h-0 w-full lg:h-full flex flex-col max-w-3xl lg:max-w-none lg:flex-none lg:w-[56%] lg:ml-auto">
              <div
                ref={lyricsScrollRef}
                className={`relative flex-1 min-h-0 overflow-y-auto overscroll-contain px-6 py-5 no-scrollbar transition-opacity duration-300 ${
                  staleLyrics ? "opacity-50" : "opacity-100"
                }`}
                style={{ zoom: lyricZoom }}
                onWheel={() => gliderRef.current?.stop()}
                onTouchStart={() => gliderRef.current?.stop()}
              >
                {displayLines.length > 0 ? (
                  displayLines.map(renderLine)
                ) : (
                  <div className="text-zinc-400 text-sm whitespace-pre-wrap leading-relaxed opacity-80">
                    {lyricsText}
                  </div>
                )}
                <div className="h-32" />
              </div>
            </div>
          )}
          {transforming && (
            <div className="absolute bottom-6 right-8 text-[10px] text-zinc-600 flex items-center gap-1">
              <span className="h-3 w-3 rounded-full border border-zinc-600 border-t-transparent animate-spin inline-block" /> transforming lyrics…
            </div>
          )}
        </div>
        )}
      </div>

      {/* up-next queue drawer — same features as the player bar's queue
          popover: CLEAR upcoming, per-track ✕, drag to reorder */}
      {queueOpen && (
        <div className="absolute top-12 right-0 bottom-0 w-80 max-w-[85vw] z-20 bg-zinc-950 flex flex-col rounded-l-2xl border-l border-t border-border">
          <div className="flex items-center justify-between px-4 py-3 border-b border-white/10 gap-2">
            <div className="text-[11px] uppercase tracking-widest text-zinc-400 min-w-0 truncate">
              Queue · {queue.length} track{queue.length === 1 ? "" : "s"}
              {queue.length > index + 1 ? ` · ${queue.length - index - 1} up next` : ""}
            </div>
            <div className="flex items-center shrink-0">
              {queue.length > index + 1 && (
                <button
                  className="px-1.5 py-1 rounded-md text-[10px] font-mono tracking-widest text-zinc-500 hover:text-white hover:bg-white/10"
                  onClick={() => setQueue(queue.slice(0, index + 1))}
                  title="Remove upcoming tracks"
                >
                  CLEAR
                </button>
              )}
              <button className="p-2 rounded-lg hover:bg-white/10 text-zinc-400 hover:text-white" onClick={() => setQueueOpen(false)} title="Close queue">
                <X className="h-5 w-5" />
              </button>
            </div>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto p-2" ref={queueListRef}>
            {queue.map((t, i) => {
              const isCurrent = i === index;
              const isDragging = dragIdx === i;
              const isOver = overIdx === i && dragIdx !== null && dragIdx !== i;
              return (
                <div
                  key={t.path + i}
                  data-queue-index={i}
                  draggable
                  onDragStart={(e) => {
                    setDragIdx(i);
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", String(i));
                  }}
                  onDragOver={(e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = "move";
                    if (overIdx !== i) setOverIdx(i);
                  }}
                  onDrop={(e) => {
                    e.preventDefault();
                    const from = dragIdx ?? Number(e.dataTransfer.getData("text/plain"));
                    if (Number.isFinite(from) && overIdx !== null && from !== overIdx) queueMove(from, overIdx);
                    setDragIdx(null);
                    setOverIdx(null);
                  }}
                  onDragEnd={() => {
                    setDragIdx(null);
                    setOverIdx(null);
                  }}
                  className={`group/qr w-full text-left px-2.5 py-2 rounded-lg flex items-center gap-3 transition-colors border-t-2 ${
                    isCurrent ? "bg-accent/15" : "hover:bg-white/10"
                  } ${isOver ? "border-accent" : "border-transparent"} ${isDragging ? "opacity-40" : ""}`}
                  title="Drag to reorder · click to play now"
                >
                  <button
                    className="min-w-0 flex-1 flex items-center gap-3 text-left"
                    onClick={() => setIndex(i)}
                    title="Play this track now"
                  >
                    <span className={`text-[10px] font-mono w-5 text-right shrink-0 ${isCurrent ? "text-accent" : "text-zinc-600"}`}>
                      {isCurrent && p.playing ? "▶" : i + 1}
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className={`block text-xs truncate ${isCurrent ? "text-white font-medium" : "text-zinc-300"}`}>
                        {t.title || t.file.replace(/\.[^.]+$/, "")}
                      </span>
                      <span className="block text-[10px] text-zinc-500 truncate">
                        {t.artist ?? t.albumPath.split("/").pop()}
                      </span>
                    </span>
                    {isCurrent && <span className="text-[10px] text-zinc-500 shrink-0">playing</span>}
                  </button>
                  <button
                    className="p-1 rounded text-zinc-600 hover:text-red-300 hover:bg-white/5 opacity-0 group-hover/qr:opacity-100 transition-opacity shrink-0"
                    onClick={() => queueRemoveAt(i)}
                    title="Remove from queue"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

    </div>
  );
}
