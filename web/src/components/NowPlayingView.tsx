import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AudioLines, Captions, ChevronDown, Heart, ListMusic, ListPlus, Pause, Play, Repeat, Settings2, Shuffle,
  SkipBack, SkipForward, Volume1, Volume2, VolumeX, X,
} from "lucide-react";
import { api } from "../api";
import VolumePct from "./VolumePct";
import { toast, useStore } from "../store";
import { fmtTech, fmtPair, isVideoFile } from "../lib/fmt";
import { AdvisoryMark } from "./Badges";
import CoverImg from "./CoverImg";
import Popover, { MenuItem } from "./Popover";
import ScrubSeek from "./ScrubSeek";
import { MAX_DB, MIN_DB, activeAnalyser } from "../lib/analyser";
import Visualizer from "./Visualizer";
import { parsePlayerLrc, activeLineRange, KaraokeWords, type LrcLine } from "./LyricsViewer";
import type { Playlist } from "../types";
import { useLyricsFollow, LYRICS_PAD_BOTTOM, LYRICS_PAD_TOP } from "../lib/lyrScroll";
import { nextSpeed, fmtSpeed } from "../lib/playback";
import { fmtDuration } from "../lib/fmt";
import useSubtitleTracks from "./SubtitledVideo";
import type { VideoAspect } from "../lib/video";

const XLIT_KEY = "mlo.np.xlit";
const TRANS_KEY = "mlo.np.trans";
const SIZE_KEY = "mlo.np.size"; // sm | md | lg
const KARAOKE_KEY = "mlo.np.karaoke"; // "1" = word-level karaoke, "0" = line highlight (default)
const ORBS_KEY = "mlo.np.orbs"; // "1" = animated background
const VIS_KEY = "mlo.np.vis"; // "1" = background pulses with the beat
const VIZ_KEY = "mlo.np.viz"; // "1" = frequency-bar visualizer visible
const ZOOM_KEY = "mlo.np.lyrzoom.v2"; // lyrics zoom multiplier (persisted)

/** Background-ambience energy window, in dBFS MEAN bin level (averaged over
 * every FFT bin, so it sits ~20 dB below the loudest band): below the floor
 * the glow is closed, at the ceiling fully open. Measured on a real track
 * this mean swings around −70 dBFS. See the ambience tick for why the mean
 * is taken in dB and not in raw bytes. */
const AMB_FLOOR_DB = -82;
const AMB_CEIL_DB = -57;

/** How often the ambience reads the analyser and updates --amb, in ms. This
 * has to keep up with `.amb-glow`'s own transitions in index.css: with the
 * old 2.6/4 s easing a 120 ms tick was more than enough, but the two low-
 * passes stacked into a layer that barely moved, so the CSS now eases in
 * under a second and the tick follows it. Still a stepped write, never a
 * per-frame paint — the smoothing below is what keeps a kick off the screen. */
const AMB_TICK_MS = 70;

/** How the player applies ReplayGain — mirrors the `replaygain_mode` config
 * options (mlo/config.py). */
type RgMode = "track" | "album" | "off";

interface Props {
  current: { path: string; file: string; albumPath: string; artist?: string; album?: string; title?: string; coverFile?: string | null; albumCover?: string | null };
  queuePos: string;
  playing: boolean;
  time: number;
  duration: number;
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
  /** Music-video presentation — the bar owns the single <video>, so the
   *  fullscreen pickers drive it through these. */
  video: {
    aspect: VideoAspect;
    /** null = as tagged (the default track), -1 = off, else that track index. */
    captions: number | null;
    onAspect: (a: VideoAspect) => void;
    onCaptions: (i: number | null) => void;
  };
  /** ReplayGain is stored in the config and applied by the player bar (it owns
   * the decoders); the fullscreen options menu is where it gets edited. */
  rg: {
    mode: RgMode;
    preamp: number;
    /** What the bar is applying to this track — null at unity. */
    applied: { gain: number; source: string | null; analyzed: boolean } | null;
    onMode: (m: RgMode) => void;
    onPreamp: (db: number) => void;
  };
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
  // Stored transliteration + translation default ON — they arrive with the
  // track's tags (or a .romaji.lrc / .<lang>.lrc sidecar) and render as
  // sub-lines under each line; nothing is generated on the fly.
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
  // ReplayGain preamp: the slider drags locally and commits to the config on a
  // short debounce, so one drag is one config write (and one re-fetch of the
  // track's gain), not one per 0.5 dB step. The commit goes through a ref —
  // the player bar re-renders several times a second, which would otherwise
  // re-arm the timer from a stale closure forever.
  const [preampDraft, setPreampDraft] = useState(p.rg.preamp);
  const rgCommit = useRef(p.rg.onPreamp);
  useEffect(() => {
    rgCommit.current = p.rg.onPreamp;
  });
  useEffect(() => setPreampDraft(p.rg.preamp), [p.rg.preamp]);
  useEffect(() => {
    if (preampDraft === p.rg.preamp) return;
    const t = setTimeout(() => rgCommit.current(preampDraft), 400);
    return () => clearTimeout(t);
  }, [preampDraft, p.rg.preamp]);
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
  // The PRIMARY text line of each block — with translations/romanization the
  // outer block also carries sub-lines, and centering the block would push
  // the sung line off the middle. The scroller centers this element.
  const primaryRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const lyricsScrollRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();

  const { time, duration } = p;
  const { queue, index, setIndex, setQueue, queueRemoveAt, queueMove, setPlaying } = useStore();
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
  // True while a word / syllable sweep is on screen — the only consumer that
  // needs 60 fps. Line changes just have to flip on the beat, so everything
  // else ticks at 20 Hz and skips ~2/3 of the full-view re-renders the pane
  // has to compete with.
  const sweepRef = useRef(false);
  useEffect(() => {
    if (!p.playing) return;
    let raf = 0;
    const tick = () => {
      const t = p.getAudioTime?.();
      if (typeof t === "number" && isFinite(t) && t >= 0) {
        setSmoothTime(sweepRef.current ? t : Math.round(t * 20) / 20);
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
  // One value comes from the audio — the shared WebAudio analyser's bass-
  // weighted level, smoothed — and it is applied as a breathing swell to ONE
  // glow layer. Everything else (cover breathing, aurora sweep, drifting color
  // fields) is CSS animation on its own long clock. The level is written as a
  // stepped custom property, not painted per frame: an instant attack on this
  // layer is what read as strobing, because a kick could brighten one frame and
  // the next took it back. The layers themselves are described at the markup
  // below.
  const ambRef = useRef<HTMLDivElement>(null);
  const eased = useRef({ energy: 0 });
  // Read through a ref, exactly like the bars do: the ambience loop already
  // handles silence internally (energy eases to 0), so `p.playing` must not be
  // an effect dependency. It was, and the teardown/rebuild on every pause
  // reset the phase to 0 — the whole background snapped to another scale in a
  // single frame on every pause and resume.
  const playingRef = useRef(p.playing);
  useEffect(() => {
    playingRef.current = p.playing;
  }, [p.playing]);
  useEffect(() => {
    const el = ambRef.current;
    if (!el) return;
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!vis || calm) {
      // One steady value: the layers keep their colors and composition, and
      // nothing moves on its own.
      el.style.setProperty("--amb", "0.45");
      return;
    }
    let timer = 0;
    let freq: Uint8Array | null = null;
    const read = () => {
      let energy = 0;
      if (playingRef.current) {
        try {
          const an = activeAnalyser();
          if (an) {
            if (!freq || freq.length !== an.frequencyBinCount) {
              freq = new Uint8Array(an.frequencyBinCount);
            }
            an.getByteFrequencyData(freq as Uint8Array<ArrayBuffer>);
            let sum = 0;
            for (let i = 0; i < freq.length; i++) sum += freq[i];
            // Bytes are the analyser's dB window (lib/analyser.ts) squeezed
            // into 0..255, so the level is read back in dB — averaging raw
            // bytes saturated on any loud master.
            const meanDb = MIN_DB + (sum / freq.length / 255) * (MAX_DB - MIN_DB);
            // The LOW bands carry the beat: 60% bass, 40% overall keeps the
            // glow following the music instead of sitting flat.
            const lowBins = Math.max(1, freq.length >> 3);
            let low = 0;
            for (let i = 0; i < lowBins; i++) low += freq[i];
            const lowDb = MIN_DB + (low / lowBins / 255) * (MAX_DB - MIN_DB);
            const mixDb = 0.6 * lowDb + 0.4 * meanDb;
            energy = Math.min(1, Math.max(0, (mixDb - AMB_FLOOR_DB) / (AMB_CEIL_DB - AMB_FLOOR_DB)));
          }
        } catch {
          energy = 0;
        }
      }
      // Asymmetric smoothing here, CSS easing on the other side: this writes
      // ONE custom property every AMB_TICK_MS and .amb-glow eases its own
      // opacity/scale from it. The old 0.12/0.03 per 120 ms stacked a ~1 s
      // attack on top of multi-second CSS transitions and flattened the whole
      // layer; these constants ride the beat (~0.16 s attack) while the
      // release stays the slower half, so nothing flashes on a kick.
      const prev = eased.current.energy;
      eased.current.energy = prev + (energy - prev) * (energy > prev ? 0.34 : 0.1);
      el.style.setProperty("--amb", eased.current.energy.toFixed(3));
      timer = window.setTimeout(read, AMB_TICK_MS);
    };
    read();
    return () => window.clearTimeout(timer);
  }, [vis]);

  // Persisted-toggle helper shared by the options menu and inline buttons.
  const persist = (key: string, v: string) => localStorage.setItem(key, v);

  // ---- video presentation (music videos in the queue) ----------------------
  // The player bar owns the popout <video> (the only decoder) and portals it
  // to <body>, restyling it to fill the viewport while this overlay is open.
  // Nothing is moved or mirrored here: a second element would double-decode,
  // and a DOM move would put React's own node under a parent its fiber tree
  // doesn't know about (NotFoundError on the next track change). All this
  // overlay adds is the click surface and the control bar painted above it.
  const videoPath = isVideoFile(p.current.file || p.current.path) ? p.current.path : null;
  // Same query key as the <video>'s own hook, so the <track> list the picker
  // shows is the one the element actually carries — no second fetch.
  const captionTracks = useSubtitleTracks(videoPath);

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
  // Stored translations / transliterations (TRANSLATION-<lang> /
  // TRANSLITERATION-<lang>-LATN tags, or .romaji.lrc / .<lang>.lrc sidecars)
  // arrive with the same payload and are parsed exactly like the original
  // lyrics, so their lines stay 1:1 with `plainLines` without re-alignment.
  // While the next track's payload loads, the PREVIOUS track's lyrics stay
  // rendered (dimmed, no highlight): resetting to empty first is what made
  // the cover jump sizes / flash to the middle on next / previous.
  useEffect(() => {
    let dead = false;
    setSmoothTime(0);
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
  }, [p.current.path]);

  const instrumental = (tags?.INSTRUMENTAL ?? "").toString().trim() === "1";
  // The lyrics on screen belong to `lyricsFor`; until the new track's
  // payload arrives they are stale — kept for layout stability, dimmed and
  // never highlighted.
  const staleLyrics = lyricsFor !== p.current.path;
  const tagsStale = tagsFor !== p.current.path;
  const lines: LrcLine[] = useMemo(
    () => (lyricsText && !instrumental ? parsePlayerLrc(lyricsText) : []),
    [lyricsText, instrumental]
  );
  // Layout (cover sizing, pane presence) follows the on-screen lyrics even
  // while stale so next/previous never reflows the whole view.
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

  // Auto-follow owns the pane — the shared controller, so this view and the
  // sidebar can't drift apart. Nothing pauses it implicitly, not even the
  // pointer resting on the lyrics (that hover-pause read as "auto-scroll
  // stopped working" whenever the cursor was parked over the pane). A wheel /
  // touch hands the pane to the reader for a few seconds; a seek, a click on
  // a line, or pressing play takes it straight back.
  const { centerLine, takeOver } = useLyricsFollow({
    active: activeLine,
    time: dispTime,
    playing: p.playing,
    scroll: lyricsScrollRef,
    rows: primaryRefs,
    reset: p.current.path,
  });

  // A word / syllable sweep on screen is the one consumer that needs the
  // clock at full rate; line changes ride the 20 Hz tick.
  const sweeping = karaoke && !!lines[aStart]?.words?.length;
  useEffect(() => {
    sweepRef.current = sweeping;
  }, [sweeping]);

  // The viewer's close handler is recreated on every parent render, so the
  // fullscreenchange listener below reads it through a ref to stay correct.
  const closeRef = useRef(p.onClose);
  closeRef.current = p.onClose;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // An inner surface gets first refusal: something that already consumed
      // the key (a capture field), or the add-to-playlist popover.
      if (e.defaultPrevented || document.querySelector("[data-lrc-editor]")) return;
      if (plOpen) {
        setPlOpen(false);
        return;
      }
      // ONE press leaves the player for good. Esc used to be spent stepping
      // out of the browser fullscreen, so closing the viewer took two presses.
      closeRef.current();
      if (document.fullscreenElement) document.exitFullscreen().catch(() => { /* gone */ });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [plOpen]);

  // Some browsers swallow Esc themselves while in native fullscreen — the page
  // never sees a keydown, only the resulting fullscreenchange. Treating that
  // transition as "the user is done" is what makes a single Esc enough
  // everywhere. The listener only exists while the viewer is mounted.
  useEffect(() => {
    const onFs = () => {
      if (!document.fullscreenElement) closeRef.current();
    };
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

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
      toast.success(`Added "${title}" to ${pl.name}`);
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlist", pl.id] });
    } catch (e) {
      toast.error(String(e));
    }
  };
  const createPlaylistAndAdd = async () => {
    const name = newPlName.trim();
    if (!name) return;
    try {
      const pl = await api.createPlaylist(name, "manual");
      await api.playlistAdd(pl.id, [p.current.path]);
      toast.success(`Added "${title}" to ${pl.name}`);
      setNewPlName("");
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlists"] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const size = LYRIC_SIZES[lyricSize];

  const renderLine = (l: LrcLine, i: number) => {
    // Every line of the current same-time cluster (duets / backing vocals)
    // reads as active; the anchor is the cluster's first line.
    const isActive = synced && aEnd >= aStart && i >= aStart && i <= aEnd;
    // Clickable only once the rows belong to the CURRENT track: while the
    // previous track's payload is still up (staleLyrics — kept on purpose for
    // layout stability) a click would seek the new track to the OLD track's
    // timestamp, and a past-the-duration target drives it straight into
    // `ended`, advancing the queue again.
    const seekable = synced && !staleLyrics;
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
        className={`py-2 ${seekable ? "cursor-pointer" : ""} ${
          isActive ? "opacity-100" : synced ? LINE_BLUR : ""
        }`}
        onClick={
          seekable
            ? () => {
                p.onSeek(l.time);
                // Center the clicked line NOW — the active-line step alone
                // misses clicks inside the line already playing.
                centerLine(i);
              }
            : undefined
        }
        title={seekable ? "Click to seek" : undefined}
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

  // What the options menu says about the gain the player is applying right now
  // — the same three cases the bar's chip covers: tags, measured on demand,
  // and unity (nothing shown in the bar).
  const rgDb = (db: number) => `${db >= 0 ? "+" : ""}${db.toFixed(1)} dB`;
  const rgLine =
    p.rg.mode === "off"
      ? "Off — every file plays at its own level."
      : p.rg.applied
        ? `${rgDb(p.rg.applied.gain)} — ${
            p.rg.applied.analyzed
              ? "measured on demand: this file has no ReplayGain tags"
              : "from ReplayGain tags"
          }${p.rg.applied.source?.endsWith("+clamp") ? "; reduced to stop clipping" : ""}`
        : "Unity — no ReplayGain for this file.";

  // Shared control blocks — the audio layout shows them under the cover;
  // the fullscreen-video layout overlays them at the bottom of the picture.
  const textBlock = (
    /* Every text row keeps a fixed height and is ALWAYS rendered —
       blanking a row while the next track's tags load is what made
       the block (and the title itself) shake on next/previous. */
    <div className="text-center w-[26rem] min-w-0">
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
      <button className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${p.shuffle ? "text-accent" : "text-zinc-500"}`} onClick={p.onToggleShuffle} title="Shuffle">
        <Shuffle className="h-4 w-4" />
      </button>
      <button className="p-2.5 rounded-lg transition-colors hover:bg-white/10 text-white" onClick={() => p.onStep(-1)} title="Previous track">
        <SkipBack className="h-5 w-5" />
      </button>
      <button
        className="p-4 rounded-lg bg-accent on-accent hover:bg-accent-soft shadow-lg transition-colors"
        onClick={p.onTogglePlay}
        title="Play / pause (Space)"
      >
        {p.playing ? <Pause className="h-6 w-6" /> : <Play className="h-6 w-6 ml-0.5" />}
      </button>
      <button className="p-2.5 rounded-lg transition-colors hover:bg-white/10 text-white" onClick={() => p.onStep(1)} title="Next track">
        <SkipForward className="h-5 w-5" />
      </button>
      <button className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${p.loop ? "text-accent" : "text-zinc-500"}`} onClick={p.onToggleLoop} title="Repeat one">
        <Repeat className="h-4 w-4" />
      </button>
      <button
        className="p-2 rounded-lg transition-colors hover:bg-white/10 text-xs font-mono text-zinc-400 min-w-[46px]"
        onClick={() => p.onSpeedChange(nextSpeed(p.speed, 1))}
        title="Playback speed — [ slower · ] faster · 0 reset to 1×"
      >
        {fmtSpeed(p.speed)}
      </button>
      {/* the divider belongs to the row's CONTROL line, not the row box:
          self-center + a fixed height keep it on the same axis as the icons
          either side of it, whatever heights they have */}
      <span className="w-px h-6 bg-white/15 mx-1 self-center shrink-0" />
      <button
        className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${p.liked ? "text-accent" : "text-zinc-500 hover:text-zinc-300"}`}
        onClick={p.onToggleLike}
        title={p.liked ? "Unlike" : "Like this track"}
      >
        <Heart className={`h-[18px] w-[18px] ${p.liked ? "fill-current" : ""}`} />
      </button>
      <div className="relative">
        <button
          className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${plOpen ? "text-accent bg-white/10" : "text-zinc-500 hover:text-zinc-300"}`}
          onClick={() => setPlOpen(!plOpen)}
          title="Add this track to a playlist"
        >
          <ListPlus className="h-[18px] w-[18px]" />
        </button>
        <Popover
          open={plOpen}
          onClose={() => setPlOpen(false)}
          align="center"
          placement="top"
          panelClass="w-60 p-1.5 max-h-72 flex flex-col"
        >
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Playlists</div>
            <div className="overflow-y-auto min-h-0">
              {(playlists ?? []).filter((pl) => pl.kind === "manual").map((pl) => (
                <MenuItem key={pl.id} label={pl.name} title={`Add to ${pl.name}`} onClick={() => addToPlaylist(pl)} />
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
        </Popover>
      </div>
    </div>
  );
  const seekRow = (
    <div className="flex items-center gap-2 text-xs text-zinc-400 w-[26rem] px-2">
      <span className="w-10 text-right font-mono tabular-nums">{fmtDuration(dispTime)}</span>
      <ScrubSeek
        videoPath={videoPath}
        value={dispTime}
        max={duration || 0}
        onChange={p.onSeek}
        className="flex-1 min-w-0"
      />
      {/* the divider sits dead-center between the duration and volume
          groups, on the same optical axis as the sliders — same height and
          margins as the transport row's, so both read as one separator. The
          duration hugs it (text-right in its fixed box): the box's trailing
          slack would otherwise push the divider visibly off-centre towards
          the volume group. */}
      <span className="w-10 text-right font-mono tabular-nums">{fmtDuration(duration)}</span>
      <span className="w-px h-6 bg-white/15 self-center shrink-0 mx-2" />
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

  // Music-video controls: caption track + how the picture fills the screen.
  // Both live in the bottom overlay with the transport, so they are one click
  // away while the picture plays (and ride the same auto-hide).
  const taggedCaption = captionTracks.find((t) => t.default);
  const videoRow = (
    <div className="flex items-center justify-center gap-4 flex-wrap">
      <label className="flex items-center gap-1.5 text-xs text-zinc-300" title="Subtitle / caption track">
        <Captions className="h-4 w-4 text-zinc-400 shrink-0" />
        <select
          className="chip text-[11px] bg-white/10 border border-white/15 text-zinc-200 px-1.5 py-1 rounded-md"
          value={p.video.captions === null ? "tagged" : String(p.video.captions)}
          onChange={(e) =>
            p.video.onCaptions(e.target.value === "tagged" ? null : Number(e.target.value))
          }
        >
          <option value="tagged">As tagged{taggedCaption ? ` — ${taggedCaption.label}` : ""}</option>
          <option value="-1">Off</option>
          {captionTracks.map((t, i) => (
            <option key={t.key} value={i}>
              {t.label}
            </option>
          ))}
        </select>
      </label>
      <div className="flex items-center gap-1 text-xs text-zinc-300" title="How the picture fills the screen">
        <span className="text-[11px] text-zinc-400 mr-0.5">Fit</span>
        {(["contain", "cover", "stretch"] as const).map((a) => (
          <button
            key={a}
            className={`chip text-[10px] border ${
              p.video.aspect === a ? "bg-accent on-accent border-accent" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"
            }`}
            onClick={() => p.video.onAspect(a)}
            title={
              a === "contain"
                ? "Whole picture, letterboxed (default)"
                : a === "cover"
                  ? "Fill the screen, cropping the overflow"
                  : "Fill the screen, ignoring the aspect ratio"
            }
          >
            {a.toUpperCase()}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <div className={`fixed inset-0 z-50 overflow-clip ${videoPath ? "bg-transparent" : "bg-zinc-950"} ${videoPath && !chromeVisible ? "cursor-none" : ""}`}>
      {/* overflow-clip (not hidden): a hidden box is still a scroll container,
          so wheel / scrollIntoView can silently scroll the whole overlay and
          leave the view "stuck" half-rendered. Clip can never be scrolled. */}
      {/* Music videos own the whole screen: the picture fills the viewport
          (object-contain over black), controls overlay the bottom edge. The
          ambience below is skipped — the video IS the background. */}
      {!videoPath && (
      <>
      {/* ---- ambient background -------------------------------------------
          Five layers, all composed here and animated by CSS on long clocks:
          the cover's own colors blurred underneath, a slow aurora sweep,
          drifting color fields, one glow the music swells (--amb, written
          every 70 ms), then grain and a vignette to settle it. The layers
          never react per frame — that was the strobing. */}
      <div ref={ambRef} className="absolute inset-0 overflow-clip" aria-hidden>
        <div className="amb-cover absolute inset-0 blur-3xl opacity-[0.34]">
          <CoverImg albumPath={p.current.albumPath} coverFile={coverFile} wrapperClass="w-full h-full" />
        </div>
        <div className="amb-sweep absolute -inset-1/2">
          <div
            className="absolute inset-0"
            style={{
              background: `conic-gradient(from 0deg at 50% 50%, transparent 0deg, rgb(${rgb.map((v) => Math.min(255, v + 30)).join(" ")} / 0.30) 70deg, transparent 150deg, rgb(${rgb.map((v) => Math.max(0, v - 25)).join(" ")} / 0.22) 250deg, transparent 330deg)`,
            }}
          />
        </div>
        {orbs && (
          <div className="absolute inset-0">
            {/* mix-blend-mode: screen adds light instead of turning muddy,
                which is what keeps overlapping fields colorful */}
            <div
              className="amb-orb amb-orb-a w-[62vw] h-[62vw] -top-[18vw] -left-[12vw]"
              style={{ background: `radial-gradient(circle at 38% 34%, rgb(${rgb.join(" ")} / 0.85), transparent 66%)` }}
            />
            <div
              className="amb-orb amb-orb-b w-[54vw] h-[54vw] bottom-[-16vw] right-[-10vw]"
              style={{ background: `radial-gradient(circle at 40% 30%, rgb(${rgb.map((v) => Math.min(255, v + 34)).join(" ")} / 0.75), transparent 64%)` }}
            />
            <div
              className="amb-orb amb-orb-c w-[44vw] h-[44vw] top-[26%] left-[34%]"
              style={{ background: `radial-gradient(circle at 60% 40%, rgb(${rgb.map((v) => Math.max(0, v - 28)).join(" ")} / 0.7), transparent 62%)` }}
            />
          </div>
        )}
        {/* the one music-driven layer: a wide soft glow behind the artwork.
            .amb-glow eases opacity/scale from --amb over ~0.6-0.9 s, so the
            music reads as the light breathing, never as a flash */}
        <div
          className="amb-glow absolute inset-0"
          style={{ background: `radial-gradient(ellipse 58% 46% at 50% 52%, rgb(${rgb.join(" ")} / 0.42), transparent 72%)` }}
        />
        <div className="amb-grain absolute inset-0" />
        <div className="amb-vignette absolute inset-0" />
      </div>
      {/* legibility wash — deliberately light so the color field stays
          visible; only the very top and bottom darken for the bars */}
      <div className="absolute inset-0 bg-gradient-to-b from-zinc-950/55 via-zinc-950/20 to-zinc-950/80" />
      </>
      )}

      {videoPath && (
        /* the picture itself is the player bar's portaled <video>, filling
           the viewport one z-step below this overlay — this layer only
           carries the click surface and the control bar */
        <div className="absolute inset-0">
          {/* click surface for play/pause — sits above the video, below
              the bottom controls, and keeps working while the chrome is
              hidden */}
          <div
            className="absolute inset-0 z-[1] cursor-pointer"
            onClick={p.onTogglePlay}
            title="Play / pause (Space)"
          />
          {/* bottom control overlay — same blocks as the audio layout; eases
              away (with the cursor) while the video plays untouched */}
          <div
            className={`absolute inset-x-0 bottom-0 z-10 bg-gradient-to-t from-black/85 via-black/45 to-transparent pt-24 pb-5 px-4 sm:px-8 transition-[opacity,transform] duration-300 ease-out ${
              chromeVisible ? "" : "pointer-events-none opacity-0 translate-y-6"
            }`}
          >
            <div className="max-w-3xl mx-auto flex flex-col items-center gap-4">
              {videoRow}
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
            className="p-2 rounded-lg transition-colors hover:bg-white/10 text-zinc-400 hover:text-white"
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
                  ? "text-zinc-500 hover:text-white hover:bg-white/10 transition-colors"
                  : "text-zinc-600 opacity-40 pointer-events-none"
              }`}
              onClick={() => setQueueOpen(true)}
              title={upNextLabel ? `Up next — ${upNextLabel} · click to view the queue` : "Up next — nothing queued"}
            >
              <span className="uppercase tracking-widest text-zinc-600 shrink-0">Up next</span>
              <span className="truncate">{upNextLabel || "—"}</span>
            </button>
            <button
              className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${queueOpen ? "text-white bg-white/10" : "text-zinc-400 hover:text-white"}`}
              onClick={() => setQueueOpen(!queueOpen)}
              title="Up next (queue)"
            >
              <ListMusic className="h-5 w-5" />
            </button>
            {/* inert over a music video: <Visualizer> only renders in the
                audio layout, so the toggle is hidden rather than a no-op */}
            {!videoPath && (
              <button
                className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${viz ? "text-accent" : "text-zinc-400 hover:text-white"}`}
                onClick={() => {
                  const v = !viz;
                  setViz(v);
                  persist(VIZ_KEY, v ? "1" : "0");
                }}
                title="Toggle visualizer bars"
              >
                <AudioLines className="h-5 w-5" />
              </button>
            )}
            <div className="relative">
              <button
                className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${options ? "text-white bg-white/10" : "text-zinc-400 hover:text-white"}`}
                onClick={() => setOptions(!options)}
                title="Lyrics & display options"
              >
                <Settings2 className="h-5 w-5" />
              </button>
              <Popover
                open={options}
                onClose={() => setOptions(false)}
                panelClass="w-72 max-w-[calc(100vw-1.5rem)] p-1.5"
              >
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Lyrics</div>
                {[
                    { id: "xlit" as const, label: "Transliteration", on: showXlit, act: () => toggleOpt("xlit") },
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
                    Background glow follows the music
                  </label>
                  {/* inert over a music video: <Visualizer> only renders in the
                      audio layout, so the toggle is hidden rather than a no-op */}
                  {!videoPath && (
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
                  )}
                  {/* loudness matching — stored in the config (Settings → DR /
                      ReplayGain) but felt right here, so it is edited here */}
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-1 pt-2 pb-1">Loudness · ReplayGain</div>
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1" title="Per track, per album, or no loudness matching at all">Gain</span>
                    {(["track", "album", "off"] as const).map((m) => (
                      <button
                        key={m}
                        className={`chip text-[10px] border ${p.rg.mode === m ? "bg-accent on-accent border-accent" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
                        onClick={() => p.rg.onMode(m)}
                        title={
                          m === "album"
                            ? "Album gain — one loudness offset for the whole album"
                            : m === "track"
                              ? "Track gain — each track matched on its own"
                              : "Off — play files at their own level"
                        }
                      >
                        {m.toUpperCase()}
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1" title="Extra gain on top of the ReplayGain value, applied to every track">Preamp</span>
                    <input
                      type="range"
                      min={-24}
                      max={24}
                      step={0.5}
                      value={preampDraft}
                      onChange={(e) => setPreampDraft(Number(e.target.value))}
                      disabled={p.rg.mode === "off"}
                      className="w-28 disabled:opacity-40"
                      title="Preamp — ±24 dB"
                    />
                    <span className="w-14 text-right text-[10px] text-zinc-500 tabular-nums">{rgDb(preampDraft)}</span>
                  </div>
                  <div className="text-[10px] text-zinc-600 px-2 pb-1">{rgLine}</div>
              </Popover>
            </div>
          </div>
        </div>

        {/* main area — music videos never reach this branch: their picture
            fills the screen behind the top bar (see the video layer above)
            with the same controls overlaid at the bottom edge */}
        {!videoPath && (
        <div className={`flex-1 min-h-0 flex flex-col lg:flex-row items-center gap-4 sm:gap-8 px-4 sm:px-8 pb-4 overflow-clip ${layoutHasLyrics ? "" : "lg:justify-center"}`}>
          {/* left column: cover, track/album/artist, all playback controls —
              centered as a group inside the full column height.
              max-h-full + overflow-y-auto: on a short window this column is
              taller than the clipped row above it, which used to silently cut
              the bottom controls (and the visualizer strip with them) off with
              no way to reach them. Capping it makes it scroll instead, and the
              strip sticks to the bottom of that scrollport so the bars are
              always visible when enabled.
              ponytail: `justify-center` in a scroll container leaves the TOP
              overflow unreachable on very short windows; move to a safe-center
              layout if anyone ever uses the player that small. */}
          <div
            className={`flex flex-col items-center justify-center gap-4 shrink-0 min-w-0 max-h-full min-h-0 overflow-x-clip overflow-y-auto ${
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
                uses; toggle via the button in the top bar or options menu.
                shrink-0 + explicit min height: this is the LAST child of a
                clipped column, and a shrinking flex item there collapsed to
                nothing (bars rendered, box clipped away). */}
            {viz && (
              <div className="sticky bottom-0 z-10 shrink-0 min-h-12 w-[26rem] px-2">
                <Visualizer playing={p.playing} className="block h-12 w-full" />
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
                onWheel={(e) => {
                  if (e.deltaY !== 0) takeOver();
                }}
                onTouchStart={takeOver}
              >
                {/* Symmetric pads so the first and last line can both reach
                    the anchor line: with a fixed tail spacer the pane ran out
                    of travel at the end of the song and looked frozen. */}
                {displayLines.length > 0 ? (
                  <>
                    <div style={{ height: LYRICS_PAD_TOP }} />
                    {displayLines.map(renderLine)}
                    <div style={{ height: LYRICS_PAD_BOTTOM }} />
                  </>
                ) : (
                  <div className="text-zinc-400 text-sm whitespace-pre-wrap leading-relaxed opacity-80">
                    {lyricsText}
                  </div>
                )}
              </div>
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
                    onClick={() => {
                      // Same as the player bar's queue popover: the track
                      // change starts playback, but the store's `playing`
                      // would stay stale — wrong icon, and the first
                      // play/pause click would be a no-op.
                      setIndex(i);
                      setPlaying(t.path);
                    }}
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
                    className="p-1 rounded text-zinc-600 hover:text-red-300 hover:bg-white/5 opacity-0 group-hover/qr:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity shrink-0"
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
