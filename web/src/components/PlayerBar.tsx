import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type SyntheticEvent } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Disc3, Heart, ListMusic, ListPlus, Maximize2, Mic2, Play, Pause, SkipBack, SkipForward, Shuffle, Repeat, Timer, Volume2, X } from "lucide-react";
import { api, isOffline } from "../api";
import { toast, useStore } from "../store";
import { fmtDuration } from "../lib/fmt";
import { fmtPair, fmtTech, isVideoFile } from "../lib/fmt";
import { nextSpeed, fmtSpeed } from "../lib/playback";
import { offlineMediaUrl } from "../lib/mediaCache";
import { heldBy, useJobLocks, useLockLabel, useLockWhy } from "../lib/locks";
import { useI18n } from "../lib/i18n";
import LockedChip from "./LockedChip";
import { AdvisoryMark } from "./Badges";
import StarRating from "./StarRating";
import { ratingOf, useRatings, useSetRating } from "../lib/ratings";
import VolumePct from "./VolumePct";
import { applyReplayGain, attachAnalyser, resumeAnalyser } from "../lib/analyser";
import NowPlayingView from "./NowPlayingView";
import LyricsSidebar from "./LyricsSidebar";
import TrackDownloadExport from "./TrackDownloadExport";
import Popover, { MenuItem } from "./Popover";
import { trackRef } from "../lib/refs";
import useSubtitleTracks from "./SubtitledVideo";
import { ASPECT_FIT, readAspect, writeAspect, type VideoAspect } from "../lib/video";

/** Mirrors the `/api/replaygain` payload (see `api.replaygain`): `gain` is the
 * dB the player applies (null = unity), `analyzed` says the backend had to
 * measure the file on the fly because its tags carried no ReplayGain. */
interface RgResult {
  path: string;
  gain: number | null;
  peak: number | null;
  mode: string;
  source: string | null;
  analyzed: boolean;
}
type RgMode = "track" | "album" | "off";

/** One line of text (the song name) that auto-scrolls back and forth ONLY
 * when it genuinely overflows the space it's given. Everything else — the
 * advisory badge, the codec readout — sits outside this window and never
 * moves. */
function ScrollingText({ text, className }: {
  text: string;
  className?: string;
}) {
  const wrapRef = useRef<HTMLSpanElement>(null);
  const textRef = useRef<HTMLSpanElement>(null);
  const [shift, setShift] = useState(0);

  useEffect(() => {
    const wrap = wrapRef.current;
    const el = textRef.current;
    if (!wrap || !el) return;
    let raf = 0;
    const measure = () => {
      // sub-pixel rounding on scaled displays can report a 1-2px phantom
      // overflow — only scroll for a real shortfall (2px+)
      const over = Math.ceil(el.scrollWidth - wrap.clientWidth);
      setShift(over > 2 ? over + 6 : 0); // +6 = visible padding at the end
    };
    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    };
    measure();
    // re-measure whenever the available space changes AND when the text's
    // own width changes (web-font swap, async badge rendering)
    const ro = new ResizeObserver(schedule);
    ro.observe(wrap);
    ro.observe(el);
    document.fonts?.ready.then(schedule).catch(() => {});
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [text]);

  const dur = Math.max(5, Math.min(24, shift / 12));
  return (
    <span ref={wrapRef} className={`block overflow-hidden min-w-0 ${className ?? ""}`}>
      <span
        ref={textRef}
        className={`block whitespace-nowrap ${shift > 0 ? "title-marquee will-change-transform" : ""}`}
        style={
          shift > 0
            ? ({
                "--title-shift": `-${shift}px`,
                animation: `title-marquee ${dur}s ease-in-out infinite`,
              } as CSSProperties)
            : undefined
        }
      >
        {text}
      </span>
    </span>
  );
}

/** How close to the start of its own track a `play` event must be to count as
 *  the track STARTING rather than the user resuming or seeking: anything past
 *  the first second of the element's audio is a resume, and counting it would
 *  turn every pause/play into another play. See PlayerBar's `countPlay`. */
const PLAY_START_SECONDS = 1;


export default function PlayerBar() {
  const queue = useStore((s) => s.queue);
  // The app's ONE lock poll (lib/locks): the player bar is always mounted, so
  // this is what keeps "which files a job holds right now" fresh everywhere —
  // the queue marks, the play guard and MAINTAIN → In progress all read it,
  // and the shared query key means they still cost one request per tick.
  useJobLocks();
  const index = useStore((s) => s.index);
  const setIndex = useStore((s) => s.setIndex);
  const setQueue = useStore((s) => s.setQueue);
  const queueRemoveAt = useStore((s) => s.queueRemoveAt);
  const queueMove = useStore((s) => s.queueMove);
  const playing = useStore((s) => s.playing);
  const setPlaying = useStore((s) => s.setPlaying);
  const queueId = useStore((s) => s.queueId);
  const vol = useStore((s) => s.vol);
  const setVol = useStore((s) => s.setVol);
  const { t } = useI18n();
  // Gapless playback: two audio elements. The idle one preloads the next
  // sequential track while the current one plays; at `ended` the elements
  // swap roles, so the next track starts without a load gap.
  const aRef = useRef<HTMLAudioElement>(null);
  const bRef = useRef<HTMLAudioElement>(null);
  const activeIsA = useRef(true);
  // Which element's src currently holds which track path — the active
  // element is resolved by PATH first: after gapless swaps / quick
  // next-previous hops the activeIsA flag alone can point at an element
  // that no longer carries the current track, which froze the lyric clock
  // (getAudioTime read an idle element stuck at 0) and killed auto-scroll
  // for every sync type.
  const pathOnA = useRef<string | null>(null);
  const pathOnB = useRef<string | null>(null);
  const setElPath = (el: HTMLAudioElement | null, path: string | null) => {
    if (el === aRef.current) pathOnA.current = path;
    else if (el === bRef.current) pathOnB.current = path;
  };
  const audio = () => {
    const p = queue[index]?.path;
    const prim = activeIsA.current ? aRef.current : bRef.current;
    const sec = activeIsA.current ? bRef.current : aRef.current;
    const pPrim = activeIsA.current ? pathOnA.current : pathOnB.current;
    const pSec = activeIsA.current ? pathOnB.current : pathOnA.current;
    if (p && pPrim === p) return prim;
    if (p && pSec === p) return sec;
    return prim;
  };
  const swapped = useRef(false); // set when the swap already advanced the queue

  const preloaded = useRef(-1); // queue index preloaded into the idle element
  const preloadedPath = useRef<string | null>(null); // what that preload holds
  const loadedPath = useRef<string | null>(null); // track the active element plays
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [shuffle, setShuffle] = useState(false);
  const [loop, setLoop] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [fullscreen, setFullscreen] = useState(false);
  // Native browser fullscreen for the fullscreen player: entering the viewer
  // also fullscreens the browser window (best effort — some embeds deny it);
  // leaving the viewer restores it.
  const openFullscreen = () => {
    setFullscreen(true);
    document.documentElement.requestFullscreen?.().catch(() => { /* denied */ });
  };
  const closeFullscreen = () => {
    setFullscreen(false);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => { /* gone */ });
  };
  const [plOpen, setPlOpen] = useState(false);
  const [queueOpen, setQueueOpen] = useState(false);
  // drag-reorder state for the queue popover (offsets within "up next")
  const [dragOff, setDragOff] = useState<number | null>(null);
  const [overOff, setOverOff] = useState<number | null>(null);
  const [lyricsOpen, setLyricsOpen] = useState(false);
  // sleep timer: an epoch-ms deadline, or "pause when this track ends"
  const [sleepOpen, setSleepOpen] = useState(false);
  const [sleepAt, setSleepAt] = useState<number | null>(null);
  const [sleepStopNext, setSleepStopNext] = useState(false);
  const [, setSleepTick] = useState(0);
  const qc = useQueryClient();

  const current = queue[index] ?? null;
  const idle = !current;
  const isVideo = !!current && (isVideoFile(current.file) || isVideoFile(current.path));
  const videoRef = useRef<HTMLVideoElement>(null);
  const media = () => (isVideo ? videoRef.current : audio()) as HTMLMediaElement | null;

  /** Report ONE play to the server (`POST /api/plays`) — the app's single play
   *  seam, for both the audio pair and the music-video popout.
   *
   *  `<audio>`/`<video>` fire `play` for a RESUME and for a SEEK as well as for
   *  a real start, so the element's own position is what tells them apart: a
   *  start is `currentTime` at (or within the first second of) the track that
   *  element holds, while resuming or seeking mid-track is nowhere near it. The
   *  last count is remembered per element+path and cleared when a new track is
   *  loaded or a repeat restarts one — so a repeat play counts, a pause/resume
   *  and a seek do not, and the gapless handover (which starts the next track
   *  on the idle element, with no load step of its own) counts once like any
   *  other start. */
  const counted = useRef<{ el: HTMLMediaElement | null; path: string | null }>({
    el: null, path: null,
  });
  const countPlay = (el: HTMLMediaElement) => {
    const path = el === videoRef.current
      ? current?.path ?? null
      : (el === aRef.current ? pathOnA.current : pathOnB.current);
    if (!path || el.currentTime > PLAY_START_SECONDS) return;
    if (counted.current.el === el && counted.current.path === path) return;
    counted.current = { el, path };
    // Fire and forget: a play is a statistic, and playback never waits on one.
    // A server that is away — or older than this build, with no route at all —
    // costs the play and nothing else.
    void api.recordPlay(path).catch(() => {});
  };
  // Music-video presentation, owned here because this is where the single
  // <video> decoder lives: how the fullscreen picture fills the screen, and
  // which caption track is showing (null = as tagged). The fullscreen overlay
  // draws the pickers and drives these through props.
  const [videoAspect, setVideoAspect] = useState<VideoAspect>(readAspect);
  const [captions, setCaptions] = useState<number | null>(null);
  // A new video starts from its own tagged caption track again.
  useEffect(() => setCaptions(null), [current?.path]);
  // The rAF lyric clock (fullscreen + sidebar) keys its effect on this
  // callback's identity; a fresh closure per render (PlayerBar re-renders
  // several times a second) tore down and rebuilt the 60 fps loop, so the
  // function reads the live element through a ref instead.
  const mediaRef = useRef(media);
  useEffect(() => {
    mediaRef.current = media;
  });
  const getAudioTime = useCallback(() => mediaRef.current()?.currentTime ?? 0, []);
  // Codec probe for the current video: `native === false` means the browser
  // cannot decode this file (container or codecs) and the player must start
  // on the live transcode instead of waiting for a playback error — this is
  // also the only path that catches decodable-video/undecodable-audio files
  // (DTS/AC-3 in Chrome), which play silently without ever erroring.
  const videoMetaQ = useQuery({
    queryKey: ["videoMeta", current?.path],
    queryFn: () => api.videoMeta(current!.path),
    enabled: isVideo && !!current,
    staleTime: 10 * 60 * 1000,
    retry: false,
  });
  const preferTranscode = isVideo && videoMetaQ.data?.native === false;
  // Per-track cover resolution: queue-carried filenames first, then the
  // library payload (covers playlists/.m3u8 queues whose entries lack them).
  const { data: libForCover } = useQuery({
    queryKey: ["library"],
    queryFn: api.library,
    staleTime: 5 * 60 * 1000,
  });
  // One Map per payload; track changes are O(1) lookups.
  const coverByPath = useMemo(() => {
    const m = new Map<string, { track: string | null; album: string | null }>();
    for (const a of libForCover?.artists ?? [])
      for (const al of a.albums)
        for (const t of al.tracks) m.set(t.path, { track: t.cover_file ?? null, album: al.cover_file ?? null });
    return m;
  }, [libForCover]);
  const libCover = current && !current.coverFile && !current.albumCover ? (coverByPath.get(current.path) ?? null) : null;
  const coverFile = current?.coverFile ?? libCover?.track ?? current?.albumCover ?? libCover?.album ?? null;
  const coverAlbumPath = current?.albumPath ?? "";

  // Queue entries built outside the library pages (e.g. .m3u8 playlist rows)
  // carry no title — fetch the tag lazily so the bar shows the song title,
  // never the file name, whenever a TITLE tag exists. Also yields the
  // MusicBrainz recording ID used for move-safe likes.
  // The rating control rides beside the advisory mark: one map for the whole
  // player, the same optimistic setter the rows use.
  const { data: ratingsData } = useRatings();
  const { setRating, pending } = useSetRating();
  const ratings = ratingsData?.ratings;

  const { data: currentTags } = useQuery({
    queryKey: ["tags", current?.path],
    queryFn: () => api.tags(current!.path),
    enabled: !!current,
    staleTime: 5 * 60 * 1000,
  });
  const displayTitle =
    current?.title || currentTags?.tags?.TITLE || (current ? current.file.replace(/\.[^.]+$/, "") : "");
  // A job claiming the file that is PLAYING never stops it: the stream already
  // has its handle, and cutting the listener off mid-track would be a worse bug
  // than the lock. The state is said out loud instead — once per track — so
  // that seeking or restarting it later is not a surprise. (The track was
  // unlocked when it started: a locked one cannot be started at all.)
  const currentLockWhy = useLockWhy(current?.path);
  const currentLockLabel = useLockLabel(current?.path);
  const noticedLock = useRef<string | null>(null);
  useEffect(() => {
    if (!current || !playing || !currentLockWhy) {
      noticedLock.current = null;
      return;
    }
    if (noticedLock.current === current.path) return;
    noticedLock.current = current.path;
    toast(t("player.locked_playing", { name: displayTitle, label: currentLockLabel }));
  }, [current, playing, currentLockWhy, currentLockLabel, displayTitle, t]);
  // The media element's duration cannot be trusted for videos: live
  // transcodes report Infinity or a FINITE-but-tiny length that creeps up
  // as MP4 fragments stream in. The ffprobe container length (tags tech /
  // video meta) is the real one, so videos prefer it; audio keeps the
  // element's own (mutagen and the element agree there).
  const probeDuration = Number((currentTags?.tech as { length?: number } | undefined)?.length ?? 0);
  const elDuration = Number.isFinite(duration) && duration > 0 ? duration : 0;
  const effDuration = isVideo ? (probeDuration || elDuration) : elDuration;
  // Ultra-condensed readout beside the title: just "16/44.1" (resolution
  // for videos). The full codec/bitrate detail stays in the tooltip and in
  // the fullscreen player's larger readouts.
  const techInfo = currentTags?.tech as
    | { codec?: string; bitrate?: number; bits_per_sample?: number; sample_rate?: number }
    | undefined;
  const techStr = fmtPair(techInfo);
  const techTip = fmtTech(techInfo);
  const [thumbFailed, setThumbFailed] = useState(false);
  useEffect(() => setThumbFailed(false), [current?.path]);
  // What plays after this track (meaningful only without shuffle) — shown
  // as a compact "UP NEXT" readout in the actions row.
  const upNextTrack = !shuffle ? queue[index + 1] : undefined;
  const upNextTitle = upNextTrack
    ? upNextTrack.title || upNextTrack.file.replace(/\.[^.]+$/, "")
    : "";
  const stepRef = useRef<(dir: 1 | -1) => void>(() => {});

  // OS-level media controls (lockscreen / media keys) — guarded, best effort.
  useEffect(() => {
    const ms = (navigator as any).mediaSession;
    if (!ms || !current) return;
    try {
      if (typeof (window as any).MediaMetadata === "function") {
        ms.metadata = new (window as any).MediaMetadata({
          title: displayTitle,
          artist: current.artist ?? "",
          album: current.album ?? "",
          artwork: [{ src: api.coverUrl(coverAlbumPath, coverFile), sizes: "512x512", type: "image/jpeg" }],
        });
      }
      ms.setActionHandler("play", () => {
        media()?.play();
        setPlaying(current.path);
      });
      ms.setActionHandler("pause", () => {
        media()?.pause();
        setPlaying(null);
      });
      ms.setActionHandler("previoustrack", () => stepRef.current(-1));
      ms.setActionHandler("nexttrack", () => stepRef.current(1));
    } catch {
      /* media session unsupported — ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, displayTitle, coverFile, coverAlbumPath]);

  const { data: likesData } = useQuery({ queryKey: ["likes"], queryFn: api.likes });
  const liked = !!current && (likesData?.paths ?? []).includes(current.path);
  const toggleLike = () => {
    if (!current) return;
    api
      .likeToggle(current.path, currentTags?.tags?.MUSICBRAINZ_TRACKID ?? undefined)
      .then((r) => {
        qc.invalidateQueries({ queryKey: ["likes"] });
        toast(`${r.liked ? "Liked" : "Unliked"} — ${displayTitle}`);
      })
      .catch((e) => toast.error(String(e)));
  };

  // ---- ReplayGain: decided BEFORE a track makes a sound ------------------
  // The gain has to be in the WebAudio stage by the time the first sample is
  // audible: starting the element at the outgoing track's gain (or unity) and
  // correcting it once /api/replaygain answers is the "really loud for a
  // second, then cut to the right level" the issue reports. So the load below
  // resolves this track's gain and only then calls play(), and the gapless
  // handover element already carries the next track's gain from its preload.
  //
  // The gain is fetched per track for the mode saved in the config (album mode
  // asks for the album gain, off means unity) — the backend already adds
  // `replaygain_preamp_db` and the clip protection, so the returned dB is
  // exactly what goes into the gain stage. Untagged tracks play at unity.
  // A settled gain is cached per path and dropped when mode or preamp changes.
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: 5 * 60 * 1000 });
  const rgModeRaw = cfg?.replaygain_mode;
  const rgMode: RgMode = rgModeRaw === "album" || rgModeRaw === "off" ? rgModeRaw : "track";
  const rgPreamp = typeof cfg?.replaygain_preamp_db === "number" ? cfg.replaygain_preamp_db : 0;
  const rgCache = useRef<Map<string, RgResult>>(new Map());
  // One in-flight request per path: the load (which needs the gain before it
  // plays) and the readout effect below share it, so awaiting a track's gain
  // costs one round trip, not two.
  const rgPending = useRef<Map<string, Promise<RgResult | null>>>(new Map());
  // Bumped whenever the cached gains stop being valid (a mode/preamp change),
  // so an answer that started under the old settings is never applied.
  const rgGen = useRef(0);
  const rgFor = (path: string): Promise<RgResult | null> => {
    const hit = rgCache.current.get(path);
    if (hit) return Promise.resolve(hit);
    const flight = rgPending.current.get(path);
    if (flight) return flight;
    // The generation this answer belongs to: a mode/preamp edit while it is
    // in flight means the value never even enters the cache.
    const gen = rgGen.current;
    const p = api
      .replaygain(path, rgMode)
      .then((r) => {
        // Unity is never cached: with on-demand analysis the backend answers
        // "not measured yet" and finishes the decode in the background, so the
        // next load asks again (one tag read) instead of pinning unity onto
        // this path for the whole session. A failure reads as unity too.
        if (r.gain !== null && rgGen.current === gen) rgCache.current.set(path, r);
        return r;
      })
      .catch(() => null)
      .finally(() => rgPending.current.delete(path));
    rgPending.current.set(path, p);
    return p;
  };
  /** Install a path's gain on whichever element holds that path — never on the
   *  other one: it may be holding the preloaded next track, and writing this
   *  track's gain there is the wrong-loudness handover. */
  const applyGainForPath = (path: string, gain: number | null) => {
    if (pathOnA.current === path && aRef.current) applyReplayGain(aRef.current, gain);
    if (pathOnB.current === path && bRef.current) applyReplayGain(bRef.current, gain);
  };
  /** Re-assert the gain that belongs to the track an element holds, read from
   *  the cache; unity when that path has no settled gain. Never another
   *  track's value — that is the loud start this stage exists to avoid.
   *  `immediate` is for the gapless handover, which calls this on the element
   *  it is about to start. */
  const applyElGain = (el: HTMLAudioElement, immediate = false) => {
    const path = el === aRef.current ? pathOnA.current : pathOnB.current;
    const hit = path ? rgCache.current.get(path) : undefined;
    applyReplayGain(el, hit?.gain ?? null, immediate);
  };
  // The result behind that gain — drives the readout beside the volume bar.
  const [rgRes, setRgRes] = useState<RgResult | null>(null);

  // Reload + play whenever the queue identity or index changes (keyed on
  // queueId so a fresh queue at the same index still reloads). Skipped when
  // the gapless swap already loaded and started the next track.
  // Music videos play through the popout <video> element (with sound) —
  // the <audio> pair stays paused so there is exactly one decoder.
  useEffect(() => {
    const track = queue[index];
    if (!track) return;
    // A pure reorder (queueMove / remove around the playing row) resolves
    // to the SAME track — reloading it would restart the song from zero.
    if (track.path === loadedPath.current) return;
    // A job is rewriting this file right now, so there is no stream to load:
    // the server answers 409 with the sentence below, and handing that to an
    // <audio> element would only be silence. `loadedPath` is deliberately NOT
    // advanced — the moment the job finishes, the same track loads normally.
    const held = heldBy(track.path);
    if (held) {
      media()?.pause();
      setPlaying(null);
      toast(held.held.why);
      return;
    }
    loadedPath.current = track.path;
    // Nothing has been counted for this track yet — the play is recorded by
    // the element's own `play` event (countPlay), so loading must not count
    // anything, only forget the previous track's count.
    counted.current = { el: null, path: null };
    const video = isVideoFile(track.file) || isVideoFile(track.path);
    setTime(0);
    setDuration(0);
    if (video) {
      // Music videos play through the popout <video> — pause the <audio>
      // pair and drop their sources so exactly one decoder exists.
      for (const a of [aRef.current, bRef.current]) {
        try {
          if (a) {
            a.pause();
            a.src = "";
          }
        } catch { /* ignore */ }
      }
      pathOnA.current = null;
      pathOnB.current = null;
      preloaded.current = -1;
      preloadedPath.current = null;
      swapped.current = false;
      // The popout's own declarative src owns loading (it carries the
      // probe-driven direct/transcode choice and remounts on path change) —
      // here we only re-apply rate/volume and nudge playback past any
      // autoplay-policy hesitation.
      const v = videoRef.current;
      if (v) {
        v.playbackRate = speed;
        v.volume = vol;
        v.play().catch(() => {});
      }
      return;
    }
    if (swapped.current) {
      swapped.current = false;
      preloaded.current = -1;
      preloadedPath.current = null;
      const el = audio();
      if (el) {
        el.playbackRate = speed;
        el.volume = vol;
        // The preloaded element fired loadedmetadata while IDLE (ignored by
        // onMeta) and won't fire again — read its duration here or the seek
        // bar stays at 0:00 for the whole track.
        if (el.duration && isFinite(el.duration)) setDuration(el.duration);
      }
      return;
    }
    preloaded.current = -1;
    preloadedPath.current = null;
    const el = audio();
    if (!el) return;
    // Exactly one decoder: the element we are NOT loading into stops here.
    // Usually it is just the idle preload slot — but when the skip lands on a
    // preloaded track (the last 10s of a song), the OLD element is the one
    // still making sound, and leaving it running played both tracks at once.
    (el === aRef.current ? bRef.current : aRef.current)?.pause();
    // The active element is whichever one the current track was just loaded
    // into — derived here (the single load point) instead of the old boolean
    // that only flipped on a gapless handover. That flag drifting is what let
    // the near-end preload overwrite the audio that was playing.
    activeIsA.current = el === aRef.current;
    // Attach here, not only in the play handler: the analyser must be live
    // for the FIRST play (a later onPlay still re-attaches the gapless
    // handover), and a track change must re-point `current` at this element
    // rather than leaving the paused one of the pair as the meter source.
    attachAnalyser(el);
    try { videoRef.current?.pause(); } catch { /* ignore */ }
    // Cache first: a downloaded track plays from Cache Storage as a blob:,
    // which needs no server, no token and no cookie — in a shell (no service
    // worker) nothing else can hand those bytes to an element. An uncached
    // path gets the stream URL exactly as before; the network case only pays
    // for one Cache Storage lookup.
    void (async () => {
      const gen = rgGen.current;
      // Both lookups run together, and the gain is INSTALLED BEFORE play():
      // see the ReplayGain block above for why an element must never start
      // on the previous track's gain and be corrected afterwards.
      const [cached, r] = await Promise.all([offlineMediaUrl(track.path), rgFor(track.path)]);
      // A quick next/previous while the lookups were in flight means a newer
      // load owns this element's src by now — that one wins.
      if (loadedPath.current !== track.path) return;
      if (!cached && isOffline()) {
        // Doomed either way, so say so instead of leaving a silent element.
        toast.error(`“${displayTitle}” isn’t downloaded — it needs the server to play.`);
      }
      el.src = cached ?? api.streamUrl(track.path);
      setElPath(el, track.path);
      el.playbackRate = speed; // fresh <src> resets the rate
      // The element is reused from the previous track, so its `paused` is not
      // a reliable "silent yet" signal here: immediate steps the gain in, and
      // the first sample of the track already carries its own value. An edit
      // made while the lookup was in flight invalidated this answer (gen) and
      // unity is the honest value — the readout effect installs the new
      // setting on the spot.
      applyReplayGain(el, rgGen.current === gen ? r?.gain ?? null : null, true);
      el.play().catch(() => {});
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, queueId]);

  // Preload the next sequential track into the idle element as the current
  // one approaches its end — this is what makes the handover gapless.
  useEffect(() => {
    if (shuffle || !current) return;
    const next = index + 1;
    if (next >= queue.length) return;
    // A reorder may have changed what "next" is — re-preload when the path
    // in the idle element no longer matches queue[next].
    if (preloaded.current === next && preloadedPath.current === queue[next].path) return;
    const nearEnd = duration > 0 && duration - time < 10;
    if (!nearEnd) return;
    const idle = activeIsA.current ? bRef.current : aRef.current;
    if (!idle) return;
    const nextPath = queue[next].path;
    // A locked next track is never preloaded: the gapless handover would start
    // it and the element would sit on a 409 as silence. Leaving the idle
    // element empty sends the end of THIS track down the normal load path,
    // which says why instead.
    if (heldBy(nextPath)) return;
    preloaded.current = next;
    preloadedPath.current = nextPath;
    // Same cache-first resolution as the audible load, or a downloaded next
    // track would hand over to a dead network instead of to its local bytes.
    // A miss lands on the stream URL, so the preload element is never pointed
    // at a blob: for a track that has no cached copy.
    void offlineMediaUrl(nextPath).then((cached) => {
      // A newer preload (or a reorder that changed what "next" is), the
      // audible load, or a quick skip past this track owns the idle element
      // by now.
      if (preloadedPath.current !== nextPath) return;
      if (!cached && isOffline()) {
        // Said here rather than at the handover: the swap into this element
        // is where the silence would start, and nothing else knows about it
        // before then.
        const t = queue[next];
        toast.error(`“${t.title || t.file.replace(/\.[^.]+$/, "")}” isn’t downloaded — it needs the server to play.`);
      }
      idle.src = cached ?? api.streamUrl(nextPath);
      setElPath(idle, nextPath);
      // The handover swaps this element in with no further load step, so the
      // next track's gain is fetched NOW — seconds of slack before it plays —
      // while the element is still paused, so the value is stepped in. After
      // a handover that already happened this lands on the playing element
      // with the value it started on, which is no audible change.
      const gen = rgGen.current;
      void rgFor(nextPath).then((r) => {
        // A mode/preamp edit while this was in flight owns the gain stage now.
        if (rgGen.current !== gen) return;
        applyGainForPath(nextPath, r?.gain ?? null);
      });
      idle.load();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [time, duration, index, queue, shuffle]);

  useEffect(() => {
    const el = media();
    if (el) el.playbackRate = speed;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speed, isVideo]);

  // Global volume: the stored value is re-applied to the ACTIVE element
  // (<video> for music videos, <audio> otherwise) whenever it changes.
  useEffect(() => {
    const el = media();
    if (el) el.volume = vol;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vol, current?.path, isVideo]);

  // A music video plays through the popout <video> (the only decoder), so
  // that element owns the analyser while a video is current. Re-attached per
  // track and per probe decision: the element is keyed on path/live, so each
  // of those is a NEW element needing its own source node.
  useEffect(() => {
    const v = videoRef.current;
    // Only once the element has a src: the popout renders with no src while
    // the offline cache lookup runs, and attaching a source node to an element
    // whose URL the guard inside attachAnalyser cannot see yet would route a
    // cross-origin stream into silence. onMeta attaches it the moment the real
    // URL (blob or network) has loaded.
    if (isVideo && v && v.src) attachAnalyser(v);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isVideo, current?.path, preferTranscode]);

  // Both mode and preamp are baked into the returned dB — a change invalidates
  // every cached gain (and the generation, so answers already in flight are
  // not applied under the new settings). The cache clear is declared before
  // the fetch effect so it runs first.
  useEffect(() => {
    rgGen.current += 1;
    rgCache.current.clear();
    rgPending.current.clear();
  }, [rgMode, rgPreamp]);
  // The gain readout for the current track — and the path a mode/preamp edit
  // takes to reach the element that is playing right now. The gain itself was
  // already installed before play by the load above; this never moves the
  // stage under a track from a stale answer.
  useEffect(() => {
    if (!current) {
      setRgRes(null);
      return;
    }
    const path = current.path;
    const gen = rgGen.current;
    const hit = rgCache.current.get(path);
    if (hit) {
      applyGainForPath(path, hit.gain);
      setRgRes(hit);
      return;
    }
    let dead = false;
    void rgFor(path).then((r) => {
      // The user moved on (or edited the settings) while this was in flight:
      // dropping it here is what keeps a slower answer for the previous track
      // from landing on the one that is playing now.
      if (dead || rgGen.current !== gen || queue[index]?.path !== path) return;
      applyGainForPath(path, r?.gain ?? null);
      setRgRes(r);
    });
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.path, rgMode, rgPreamp]);

  // Mode/preamp edits from the fullscreen options menu — persisted into the
  // config (a whole-document POST, hence the spread); the refetched config
  // feeds rgMode/rgPreamp above, which re-fetches the gain for this track.
  const saveRg = (patch: { replaygain_mode?: RgMode; replaygain_preamp_db?: number }) => {
    if (!cfg) return;
    api
      .saveConfig({ ...cfg, ...patch })
      .then(() => qc.invalidateQueries({ queryKey: ["config"] }))
      .catch((e) => toast.error(String(e)));
  };
  // The bar's gain readout: a number whenever a gain actually applies (tags or
  // measured on demand), and NOTHING at unity — a "0 dB" chip would claim the
  // loudness was matched when nothing was.
  const rgGain = rgRes?.gain ?? null;
  const fmtDb = (db: number) => `${db >= 0 ? "+" : ""}${db.toFixed(1)} dB`;
  const rgTip =
    rgGain === null
      ? ""
      : `ReplayGain ${fmtDb(rgGain)} (${rgMode}) — ${
          rgRes?.analyzed
            ? "measured on demand: this file has no ReplayGain tags"
            : "from ReplayGain tags"
        }${rgRes?.source?.endsWith("+clamp") ? "; reduced to stop clipping" : ""}`;

  // The fullscreen viewer opens from the player's own button AND from the
  // app-wide "F" shortcut. The state lives here (this is where the decoders
  // are), so the shortcut arrives as an event rather than reaching into it —
  // and a stray press with nothing queued says so instead of opening an empty
  // viewer.
  const fullscreenRef = useRef(false);
  useEffect(() => {
    fullscreenRef.current = fullscreen;
  }, [fullscreen]);
  useEffect(() => {
    const toggle = () => {
      if (fullscreenRef.current) {
        closeFullscreen();
        return;
      }
      if (!current) {
        toast("Play a track first — the fullscreen viewer shows what is playing");
        return;
      }
      openFullscreen();
    };
    window.addEventListener("mlo:fullscreen-toggle", toggle);
    return () => window.removeEventListener("mlo:fullscreen-toggle", toggle);
  }, [current]);

  // Keyboard shortcuts: Space pause/play · [ / ] speed down/up · 0 reset ·
  // ← / → seek ±5s. Never hijacks typing or the lyrics editor (which owns
  // Space while stamping).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t.isContentEditable) return;
      const code = e.code;
      if (code === "Space") {
        if (document.querySelector("[data-lrc-editor]")) return;
        e.preventDefault();
        const a = media();
        if (!a || !current) return;
        if (playing) {
          a.pause();
          setPlaying(null);
        } else {
          a.play().catch(() => {});
          setPlaying(current.path);
        }
      } else if (code === "BracketLeft") {
        if (document.querySelector("[data-lrc-editor]")) return; // the lyrics editor owns its own speed bindings
        setSpeed((s) => Math.max(0.5, Math.round((s - 0.25) * 100) / 100));
      } else if (code === "BracketRight") {
        if (document.querySelector("[data-lrc-editor]")) return;
        setSpeed((s) => Math.min(2, Math.round((s + 0.25) * 100) / 100));
      } else if (code === "Digit0") {
        setSpeed(1);
      } else if (code === "ArrowLeft") {
        if (document.querySelector("[data-lrc-editor]")) return; // lyrics editor owns seeking
        const a = media();
        if (a) a.currentTime = Math.max(0, a.currentTime - 5);
      } else if (code === "ArrowRight") {
        if (document.querySelector("[data-lrc-editor]")) return;
        const a = media();
        if (a && a.duration) a.currentTime = Math.min(a.duration, a.currentTime + 5);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [playing, current]);

  const cycleSpeed = () => setSpeed((s) => nextSpeed(s, 1));

  const { data: playlists } = useQuery({
    queryKey: ["playlists"],
    queryFn: api.playlists,
    enabled: plOpen,
  });
  const addToPlaylist = async (pid: number, name: string) => {
    if (!current) return;
    try {
      await api.playlistAdd(pid, [current.path]);
      toast.success(`Added “${displayTitle}” to ${name}`);
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlists"] });
    } catch (e) {
      toast.error(String(e));
    }
  };
  const newPlaylistAndAdd = async () => {
    if (!current) return;
    const name = window.prompt("New playlist name");
    if (!name?.trim()) return;
    try {
      const pl = await api.createPlaylist(name.trim(), "manual");
      await api.playlistAdd(pl.id, [current.path]);
      toast.success(`Added “${displayTitle}” to ${name.trim()}`);
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlists"] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const step = (dir: 1 | -1) => {
    const n = queue.length;
    if (!n) return;
    resumeAnalyser();
    let next: number;
    if (shuffle) {
      next = Math.floor(Math.random() * n);
      if (n > 1) while (next === index) next = Math.floor(Math.random() * n);
    } else {
      next = (index + dir + n) % n;
    }
    setIndex(next);
    setPlaying(queue[next]?.path ?? null);
  };
  stepRef.current = step;

  // ---- sleep timer ---------------------------------------------------------
  const SLEEP_CHOICES = [5, 15, 30, 45, 60];
  const armSleep = (mins: number) => {
    setSleepAt(Date.now() + mins * 60000);
    setSleepStopNext(false);
    setSleepOpen(false);
    toast(`Sleep timer — pausing in ${mins} min`);
  };
  const armSleepEndOfTrack = () => {
    setSleepStopNext(true);
    setSleepAt(null);
    setSleepOpen(false);
    toast("Sleep timer — pausing after this track");
  };
  const cancelSleep = () => {
    setSleepAt(null);
    setSleepStopNext(false);
    setSleepOpen(false);
    toast("Sleep timer cancelled");
  };
  const sleepRemaining = sleepAt !== null ? Math.max(0, sleepAt - Date.now()) : null;
  const fmtRemaining = (ms: number) => {
    const s = Math.ceil(ms / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = s % 60;
    return h
      ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`
      : `${m}:${String(ss).padStart(2, "0")}`;
  };
  // countdown ticker + the actual pause when the deadline passes
  useEffect(() => {
    if (sleepAt === null) return;
    const iv = setInterval(() => {
      if (Date.now() >= sleepAt) {
        media()?.pause();
        useStore.getState().setPlaying(null);
        setSleepAt(null);
        toast("Sleep timer — playback paused");
      }
      setSleepTick((t) => t + 1);
    }, 1000);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sleepAt]);

  // Fired by whichever element is active when its track ends. The gapless
  // path swaps the preloaded idle element in and starts it immediately —
  // no network fetch, no decode pause.
  const handleEnded = (e?: SyntheticEvent<HTMLMediaElement>) => {
    // Only the ACTIVE decoder may advance the queue: one of the audio pair,
    // or the music-video popout (which never matches audio(), so it needs
    // its own check — without it videos would end and play nothing next).
    if (e && e.currentTarget !== audio() && e.currentTarget !== videoRef.current) return;
    if (sleepStopNext) {
      media()?.pause();
      useStore.getState().setPlaying(null);
      setSleepStopNext(false);
      toast("Sleep timer — playback paused");
      return;
    }
    if (loop) {
      const m = media();
      if (m) {
        // A repeat is a NEW play: forget the count so the element's own `play`
        // event (currentTime back at 0) records this one too.
        counted.current = { el: null, path: null };
        m.currentTime = 0;
        m.play().catch(() => {});
      }
      return;
    }
    const next = index + 1;
    // End of the queue, no shuffle, no repeat: the queue popover promises
    // "it ends after this track", so it ends — pausing instead of silently
    // wrapping to track 1. The explicit Next button still wraps (step()).
    if (!shuffle && next >= queue.length) {
      media()?.pause();
      setPlaying(null);
      return;
    }
    if (!shuffle && next < queue.length && preloaded.current === next) {
      swapped.current = true;
      // The handover element is the one that is NOT holding the track that
      // just finished — the preload wrote the incoming track there. Resolving
      // through audio() here was wrong: it resolves against queue[index] and
      // this tick still has the OLD index, so it returned the DEAD element
      // and play() restarted the finished song from 0 while the bar showed the
      // next row (and the load effect then consumed `swapped` without loading
      // the real next track — silence, frozen clock, sticky).
      const el = pathOnA.current === queue[index]?.path ? bRef.current : aRef.current;
      // Keep the flag consistent with whichever element actually plays.
      activeIsA.current = el === aRef.current;
      setIndex(next);
      setPlaying(queue[next]?.path ?? null);
      setTime(0);
      const d = el?.duration;
      setDuration(d !== undefined && Number.isFinite(d) ? d : 0);
      if (el) {
        el.playbackRate = speed;
        el.volume = vol;
        // This element was loaded by the preload, which installed the next
        // track's gain on it — re-assert it from the cache first anyway: the
        // handover is the one place a track starts with no load step in
        // between, so a cold cache entry here would be an audible burst.
        applyElGain(el, true);
        el.play().catch(() => {});
      }
      return;
    }
    step(1);
  };

  // time/metadata events fire on both elements; only the active one drives
  // the UI (the idle element's preloaded metadata must not touch the bar).
  // The video popout drives the same state when a music video is playing.
  const onTime = (e: SyntheticEvent<HTMLAudioElement>) => {
    if (e.currentTarget === audio()) setTime(e.currentTarget.currentTime);
  };
  const onMeta = (e: SyntheticEvent<HTMLAudioElement>) => {
    const d = e.currentTarget.duration;
    if (e.currentTarget === audio()) setDuration(Number.isFinite(d) ? d : 0);
  };
  const onVideoTime = (e: SyntheticEvent<HTMLVideoElement>) => {
    setTime(e.currentTarget.currentTime);
  };
  const onVideoMeta = (e: SyntheticEvent<HTMLVideoElement>) => {
    // Fragmented-MP4 live transcodes report Infinity — 0 lets the probed
    // container duration take over (effDuration).
    const d = e.currentTarget.duration;
    setDuration(Number.isFinite(d) ? d : 0);
  };

  const togglePlay = () => {
    const a = media();
    if (!a || !current) return;
    resumeAnalyser();
    if (playing) {
      a.pause();
      setPlaying(null);
    } else {
      a.play().catch(() => {});
      setPlaying(current.path);
    }
  };

  // ONE bar for both states — same height, radius and layout whether or not
  // something is playing; idle just disables the transport and shows a hint.
  return (
    <div className="shrink-0 px-3 pb-3 pt-1 relative z-10">
      <div className="h-[4.75rem] rounded-lg border border-border bg-panel shadow-lg shadow-black/40">
        {/* crossOrigin keeps the streams CORS-clean so the WebAudio visualizer
            can read them; attachAnalyser resumes the context it opens, so the
            very first play is not read from a suspended (all-zero) graph */}
        <audio ref={aRef} hidden crossOrigin="anonymous" onTimeUpdate={onTime} onLoadedMetadata={onMeta} onEnded={handleEnded}
          onPlay={(e) => { attachAnalyser(e.currentTarget); applyElGain(e.currentTarget); countPlay(e.currentTarget); }} />
        <audio ref={bRef} hidden crossOrigin="anonymous" onTimeUpdate={onTime} onLoadedMetadata={onMeta} onEnded={handleEnded}
          onPlay={(e) => { attachAnalyser(e.currentTarget); applyElGain(e.currentTarget); countPlay(e.currentTarget); }} />

        {/* full layout from tablet width up: cover+title / centered seek /
            actions+volume, balanced 1fr-auto-1fr so the seek bar sits dead
            center */}
        <div className="hidden md:grid h-full grid-cols-[1fr_auto_1fr] items-center gap-3 [container-type:inline-size]">
        {/* left flank of the grid: cover + title block — its 1fr track
            balances the right cluster so the seek bar sits dead center.
            The cover is absolutely positioned so its image's INTRINSIC
            size never inflates the grid row height. */}
        <div className="relative flex items-center gap-3 min-w-0 self-stretch">
        <button
          className={`absolute inset-y-0 left-0 aspect-square rounded-l-[5px] overflow-hidden bg-raise shrink-0 flex items-center justify-center ${
            idle ? "cursor-default" : "group/cover"
          }`}
          onClick={() => !idle && openFullscreen()}
          title={idle ? "Nothing playing" : "Album art — click for the fullscreen player"}
          disabled={idle}
        >
          {current && !thumbFailed ? (
            <img
              src={api.coverUrl(coverAlbumPath, coverFile)}
              alt=""
              onError={() => setThumbFailed(true)}
              className="h-full w-full object-cover"
            />
          ) : (
            <Disc3 className={`h-5 w-5 ${idle ? "text-zinc-700" : "text-zinc-600"}`} />
          )}
        </button>

        <div className="min-w-0 flex-1 ml-[76px] pl-3" title={current ? [current.artist, current.album].filter(Boolean).join(" · ") : undefined}>
          {current ? (
            <>
              {/* badge + tech readout hug the title: the window only takes
                  the width the text needs, and shrinks (marquee) when the
                  name is too long — they never get pushed to the edge */}
              <div className="flex items-baseline gap-2 min-w-0">
                {/* title opens the track's own page (tag editing, links, lyrics) */}
                <Link
                  to={trackRef({ path: current.path, tags: { MUSICBRAINZ_TRACKID: currentTags?.tags?.MUSICBRAINZ_TRACKID } })}
                  className="min-w-0 hover:[&>span]:text-accent-soft transition-colors"
                  title="Open the track page"
                >
                  <ScrollingText text={displayTitle} />
                </Link>
                <AdvisoryMark value={currentTags?.tags?.ITUNESADVISORY ?? current.advisory} />
                <StarRating
                  size="sm"
                  className="self-center"
                  value={ratingOf(ratings, current.path)}
                  onChange={(v) => setRating(current.path, v)}
                  pending={pending(current.path)}
                />
                {techStr && (
                  <span className="text-[10px] font-mono text-zinc-500 shrink-0" title={techTip || "Bit depth/sample rate"}>
                    {techStr}
                  </span>
                )}
              </div>
              <div className="text-[11px] text-zinc-500 truncate">{current.album ?? "—"}</div>
              <div className="text-[11px] text-zinc-500 truncate">{current.artist ?? current.albumPath.split("/").pop()}</div>
            </>
          ) : (
            <>
              <div className="text-sm truncate font-semibold text-zinc-500">Nothing playing</div>
              <div className="text-[11px] text-zinc-600 truncate">
                Play an album, artist or playlist to start
              </div>
            </>
          )}
        </div>
        </div>

        {/* center of the grid: seek bar above the transport controls — the
            1fr tracks on both sides keep it dead center of the bar */}
        <div className="min-w-0 shrink flex flex-col items-center justify-center gap-0.5 w-[min(28cqw,32rem)] lg:w-[min(34cqw,40rem)]">
          <div className="flex items-center gap-2 w-full max-w-2xl mx-auto text-[10px] text-zinc-500 tabular-nums">
            <span className="w-10 text-right shrink-0">{fmtDuration(time)}</span>
            <input
              type="range"
              min={0}
              max={effDuration || 0}
              step={0.05}
              value={Math.min(time, effDuration || 0)}
              onChange={(e) => {
                const a = media();
                if (!a) return;
                a.currentTime = Number(e.target.value);
                setTime(Number(e.target.value));
              }}
              className="flex-1 min-w-0 seek-fat"
              disabled={idle}
              title="Seek — ← / → nudge 5s"
            />
            <span className="w-10 shrink-0">{fmtDuration(effDuration)}</span>
          </div>
          <div className="flex items-center gap-0.5">
            {/* like — far LEFT of the transport, mirroring the speed chip on
                the far right */}
            <button
              className={`p-2 rounded-lg hover:bg-raise min-w-[46px] flex items-center justify-center shrink-0 ${
                liked ? "text-accent" : "text-zinc-500 hover:text-zinc-300"
              } ${idle ? "opacity-40 pointer-events-none" : ""}`}
              onClick={toggleLike}
              disabled={idle}
              title={liked ? "Unlike" : "Like this track"}
            >
              <Heart className={`h-4 w-4 ${liked ? "fill-current" : ""}`} />
            </button>
            <button
              className={`p-2 rounded-lg hover:bg-raise ${shuffle ? "text-accent" : "text-zinc-500"}`}
              onClick={() => setShuffle(!shuffle)}
              disabled={idle}
              title="Shuffle"
            >
              <Shuffle className="h-4 w-4" />
            </button>
            <button className="p-2 rounded-lg hover:bg-raise text-zinc-300" onClick={() => step(-1)} disabled={idle} title="Previous track">
              <SkipBack className="h-4 w-4" />
            </button>
            <button
              className={`p-2.5 rounded-lg bg-accent on-accent hover:bg-accent-soft active:scale-95 transition-transform ${
                idle ? "opacity-40 pointer-events-none" : ""
              }`}
              onClick={togglePlay}
              disabled={idle}
            >
              {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
            </button>
            <button className="p-2 rounded-lg hover:bg-raise text-zinc-300" onClick={() => step(1)} disabled={idle} title="Next track">
              <SkipForward className="h-4 w-4" />
            </button>
            <button
              className={`p-2 rounded-lg hover:bg-raise ${loop ? "text-accent" : "text-zinc-500"}`}
              onClick={() => setLoop(!loop)}
              disabled={idle}
              title="Repeat one"
            >
              <Repeat className="h-4 w-4" />
            </button>
            <button
              className={`p-1.5 rounded-lg hover:bg-raise text-xs font-mono text-zinc-400 min-w-[46px] ${
                idle ? "opacity-40 pointer-events-none" : ""
              }`}
              onClick={cycleSpeed}
              disabled={idle}
              title="Playback speed — [ slower · ] faster · 0 reset to 1×"
            >
              {fmtSpeed(speed)}
            </button>
          </div>
        </div>

        {/* right flank of the grid: actions row + volume, then lyrics /
            fullscreen stacked on the far right */}
        <div className="flex items-center gap-2 shrink min-w-0 justify-self-end w-full justify-end pr-4">
          <div className="flex flex-col items-center gap-0.5 min-w-0 shrink">
            <div className="flex items-center gap-0.5">
              {/* up next — mirrors the fullscreen player's top-bar readout;
                  opens the same queue popover. Always on the bar: inert
                  (like the rest) when there is nothing queued. */}
              <button
                className={`hidden lg:flex items-center gap-1.5 px-1.5 py-1 rounded-md font-mono text-[10px] tabular-nums min-w-0 max-w-[13rem] shrink ${
                  upNextTrack
                    ? "text-zinc-500 hover:text-white hover:bg-raise"
                    : "text-zinc-600 opacity-40 pointer-events-none"
                }`}
                onClick={() => setQueueOpen(true)}
                title={
                  upNextTrack
                    ? `Up next — ${upNextTitle}${upNextTrack.artist ? ` — ${upNextTrack.artist}` : ""} · click to view the queue`
                    : "Up next — nothing queued"
                }
              >
                <span className="uppercase tracking-widest text-zinc-600 shrink-0">Up next</span>
                <span className="truncate">{upNextTitle || "—"}</span>
              </button>
              {/* queue position — the fraction lives here, left of the playlist
                  button; clicking it (or the queue button) opens the queue */}
              <button
                className={`px-1.5 py-1 rounded-md font-mono text-[10px] tabular-nums shrink-0 transition-colors ${
                  queueOpen ? "text-accent bg-raise" : "text-zinc-500 hover:text-white hover:bg-raise"
                } ${current && queue.length > 1 ? "" : "opacity-40 pointer-events-none"}`}
                onClick={() => setQueueOpen(!queueOpen)}
                title={
                  current && queue.length > 1
                    ? `Queue position — ${index + 1} of ${queue.length} · click to view the queue`
                    : "Queue position — nothing playing"
                }
              >
                {current && queue.length > 1 ? `${index + 1}/${queue.length}` : "–/–"}
              </button>

              {/* queue popover: upcoming tracks, click to jump, ✕ to remove */}
              <div className="relative">
                <button
                  className={`p-2 rounded-lg hover:bg-raise shrink-0 ${
                    queueOpen ? "text-accent bg-raise" : "text-zinc-400 hover:text-white"
                  } ${idle ? "opacity-40 pointer-events-none" : ""}`}
                  onClick={() => setQueueOpen(!queueOpen)}
                  disabled={idle}
                  title="Queue"
                  aria-label="Queue"
                >
                  <ListMusic className="h-4 w-4" />
                </button>
                <Popover
                  open={queueOpen}
                  onClose={() => setQueueOpen(false)}
                  placement="top"
                  panelClass="w-80 p-1.5 max-h-80 overflow-auto"
                >
                      <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1 flex items-center justify-between gap-2">
                        <span>
                          Queue{queue.length > index + 1 ? ` · ${queue.length - index - 1} up next` : ""}
                        </span>
                        {queue.length > index + 1 && (
                          <button
                            className="text-[10px] font-mono tracking-widest text-zinc-500 hover:text-white"
                            onClick={() => setQueue(queue.slice(0, index + 1))}
                            title="Remove upcoming tracks"
                          >
                            CLEAR
                          </button>
                        )}
                      </div>
                      {current && (
                        <div className="px-2 py-1.5 rounded-md bg-raise/60 flex items-center gap-2">
                          <Play className="h-3 w-3 text-accent shrink-0" />
                          <span className="text-xs text-zinc-200 truncate flex-1">{displayTitle}</span>
                          <span className="text-[10px] text-zinc-600 shrink-0">playing</span>
                        </div>
                      )}
                      {queue.slice(index + 1).map((t, off) => {
                        const i = index + 1 + off;
                        const isDragging = dragOff === off;
                        const isOver = overOff === off && dragOff !== null && dragOff !== off;
                        return (
                          <div
                            key={`${t.path}-${i}`}
                            draggable
                            onDragStart={(e) => {
                              setDragOff(off);
                              e.dataTransfer.effectAllowed = "move";
                              e.dataTransfer.setData("text/plain", String(off));
                            }}
                            onDragOver={(e) => {
                              e.preventDefault();
                              e.dataTransfer.dropEffect = "move";
                              if (overOff !== off) setOverOff(off);
                            }}
                            onDrop={(e) => {
                              e.preventDefault();
                              const from = dragOff ?? Number(e.dataTransfer.getData("text/plain"));
                              if (Number.isFinite(from) && overOff !== null && from !== overOff)
                                queueMove(index + 1 + from, index + 1 + overOff);
                              setDragOff(null);
                              setOverOff(null);
                            }}
                            onDragEnd={() => {
                              setDragOff(null);
                              setOverOff(null);
                            }}
                            className={`group/qr flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 border-t-2 ${
                              isOver ? "border-accent" : "border-transparent"
                            } ${isDragging ? "opacity-40" : ""}`}
                            title="Drag to reorder · click to play now"
                          >
                            <button
                              className="min-w-0 flex-1 text-left"
                              onClick={() => {
                                // A locked row says why instead of jumping to a
                                // track the server would refuse to stream.
                                const held = heldBy(t.path);
                                if (held) {
                                  toast(held.held.why);
                                  return;
                                }
                                setIndex(i);
                                setPlaying(t.path);
                                setQueueOpen(false);
                              }}
                              title="Play this track now"
                            >
                              <div className="flex items-center gap-1.5 min-w-0">
                                <span className="text-xs text-zinc-300 truncate">
                                  {t.title || t.file.replace(/\.[^.]+$/, "")}
                                </span>
                                <LockedChip path={t.path} />
                              </div>
                              <div className="text-[10px] text-zinc-600 truncate">
                                {[t.artist, t.album].filter(Boolean).join(" · ")}
                              </div>
                            </button>
                            <button
                              className="p-1 rounded text-zinc-600 hover:text-red-300 hover:bg-white/5 opacity-0 group-hover/qr:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity shrink-0"
                              onClick={() => queueRemoveAt(i)}
                              title="Remove from queue"
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </div>
                        );
                      })}
                      {queue.length <= index + 1 && (
                        <div className="text-[10px] text-zinc-600 px-2 py-1">Nothing up next — it ends after this track.</div>
                      )}
                </Popover>
              </div>

              {/* sleep timer */}
              <div className="relative">
                <button
                  className={`p-2 rounded-lg hover:bg-raise shrink-0 flex items-center gap-1 ${
                    sleepAt !== null || sleepStopNext
                      ? "text-accent bg-raise"
                      : "text-zinc-400 hover:text-white"
                  } ${idle ? "opacity-40 pointer-events-none" : ""}`}
                  onClick={() => setSleepOpen(!sleepOpen)}
                  disabled={idle}
                  title={sleepAt !== null ? `Sleep timer — ${fmtRemaining(sleepRemaining ?? 0)} left` : sleepStopNext ? "Sleep timer — stops after this track" : "Sleep timer"}
                  aria-label="Sleep timer"
                >
                  <Timer className="h-4 w-4" />
                  {sleepAt !== null && (
                    <span className="text-[10px] font-mono tabular-nums">{fmtRemaining(sleepRemaining ?? 0)}</span>
                  )}
                  {sleepStopNext && <span className="text-[10px] font-mono">1t</span>}
                </button>
                <Popover
                  open={sleepOpen}
                  onClose={() => setSleepOpen(false)}
                  placement="top"
                  panelClass="w-48 p-1.5"
                >
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Sleep timer</div>
                  <MenuItem label="After this track" onClick={armSleepEndOfTrack} />
                  {SLEEP_CHOICES.map((m) => (
                    <MenuItem key={m} label={`${m} minutes`} onClick={() => armSleep(m)} />
                  ))}
                  {(sleepAt !== null || sleepStopNext) && (
                    <MenuItem label="Cancel timer" danger onClick={cancelSleep} />
                  )}
                </Popover>
              </div>

              <div className="relative">
                <button
                  className={`p-2 rounded-lg hover:bg-raise shrink-0 ${
                    plOpen ? "text-accent bg-raise" : "text-zinc-400 hover:text-white"
                  } ${idle ? "opacity-40 pointer-events-none" : ""}`}
                  onClick={() => setPlOpen(!plOpen)}
                  disabled={idle}
                  title="Add to playlist"
                  aria-label="Add to playlist"
                >
                  <ListPlus className="h-4 w-4" />
                </button>
                <Popover
                  open={plOpen}
                  onClose={() => setPlOpen(false)}
                  placement="top"
                  panelClass="w-56 p-1.5 max-h-64 overflow-auto"
                >
                      <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Add to playlist</div>
                      <button
                        className="w-full text-left text-xs px-2 py-1.5 rounded-md hover:bg-white/10 text-accent-soft"
                        onClick={newPlaylistAndAdd}
                      >
                        <ListPlus className="h-3.5 w-3.5 inline mr-1.5 -mt-0.5" /> New playlist…
                      </button>
                      {(playlists ?? []).map((p) => (
                        <button
                          key={p.id}
                          className="w-full text-left text-xs px-2 py-1.5 rounded-md hover:bg-white/10 text-zinc-300 flex items-center justify-between gap-2"
                          onClick={() => addToPlaylist(p.id, p.name)}
                        >
                          <span className="truncate">{p.name}</span>
                          <span className="text-[10px] text-zinc-600 shrink-0">{p.track_count}</span>
                        </button>
                      ))}
                      {(playlists ?? []).length === 0 && (
                        <div className="text-[10px] text-zinc-600 px-2 py-1">No playlists yet — create one above.</div>
                      )}
                </Popover>
              </div>

              <TrackDownloadExport path={current?.path ?? ""} iconOnly disabled={!current} up />
            </div>

            {/* layer 2: the volume bar beneath the buttons — hidden on tablet
                (md–lg) so the center seek never collides with the cluster */}
            <div className="hidden lg:flex items-center gap-1.5 w-full px-2 text-zinc-400" title={`Volume — ${Math.round(vol * 100)}%`}>
              <Volume2 className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={vol}
                onChange={(e) => setVol(Number(e.target.value))}
                className="flex-1 min-w-0 seek-fat"
                title="Volume — shared by the whole app"
              />
              <VolumePct value={vol} onChange={setVol} />
              {/* the applied ReplayGain, right where the level is set — the
                  number is the dB the player is adding, the tooltip says where
                  it came from. Absent entirely at unity. */}
              {rgGain !== null && (
                <span className="text-[10px] font-mono tabular-nums shrink-0 text-zinc-500 cursor-help" title={rgTip}>
                  RG {fmtDb(rgGain)}
                </span>
              )}
            </div>
          </div>

          {/* far right: lyrics stacked on top of fullscreen */}
          <div className="flex flex-col items-center gap-0.5 shrink-0 self-stretch justify-center">
            <button
              className={`p-2 rounded-lg hover:bg-raise shrink-0 ${
                lyricsOpen ? "text-accent bg-raise" : "text-zinc-400 hover:text-white"
              }`}
              onClick={() => setLyricsOpen(!lyricsOpen)}
              title="Lyrics — open the sidebar"
              aria-label="Lyrics"
            >
              <Mic2 className="h-4 w-4" />
            </button>

            <button
              className={`p-2 rounded-lg hover:bg-raise text-zinc-400 hover:text-white shrink-0 ${
                idle ? "opacity-40 pointer-events-none" : ""
              }`}
              onClick={() => openFullscreen()}
              disabled={idle}
              title="Fullscreen player with lyrics"
            >
              <Maximize2 className="h-4 w-4" />
            </button>
          </div>
        </div>
        </div>

        {/* phone layout: cover · title · like/play/next/fullscreen — the
            transport that fits a thumb, no seek row (drag in the fullscreen
            player); cover is a plain flex item here, not absolute */}
        <div className="flex md:hidden h-full items-center gap-1 pr-2">
          <button
            className="self-stretch aspect-square rounded-l-[5px] overflow-hidden bg-raise shrink-0 flex items-center justify-center"
            onClick={() => !idle && openFullscreen()}
            title={idle ? "Nothing playing" : "Album art — tap for the fullscreen player"}
            disabled={idle}
          >
            {current && !thumbFailed ? (
              <img src={api.coverUrl(coverAlbumPath, coverFile)} alt="" onError={() => setThumbFailed(true)} className="h-full w-full object-cover" />
            ) : (
              <Disc3 className={`h-5 w-5 ${idle ? "text-zinc-700" : "text-zinc-600"}`} />
            )}
          </button>
          <div className="min-w-0 flex-1 pl-2" title={current ? [current.artist, current.album].filter(Boolean).join(" · ") : undefined}>
            {current ? (
              <>
                <div className="flex items-baseline gap-1.5 min-w-0">
                  <Link
                    to={trackRef({ path: current.path, tags: { MUSICBRAINZ_TRACKID: currentTags?.tags?.MUSICBRAINZ_TRACKID } })}
                    className="min-w-0 hover:[&>span]:text-accent-soft transition-colors"
                    title="Open the track page"
                  >
                    <ScrollingText text={displayTitle} />
                  </Link>
                  <AdvisoryMark value={currentTags?.tags?.ITUNESADVISORY ?? current.advisory} />
                </div>
                <div className="text-[11px] text-zinc-500 truncate">
                  {[current.artist ?? current.albumPath.split("/").pop(), current.album].filter(Boolean).join(" · ") || "—"}
                </div>
              </>
            ) : (
              <div className="text-sm truncate font-semibold text-zinc-500">Nothing playing</div>
            )}
          </div>
          <button
            className={`tap-hit p-2 rounded-lg hover:bg-raise shrink-0 ${liked ? "text-accent" : "text-zinc-500"} ${idle ? "opacity-40 pointer-events-none" : ""}`}
            onClick={toggleLike}
            disabled={idle}
            title={liked ? "Unlike" : "Like this track"}
          >
            <Heart className={`h-4 w-4 ${liked ? "fill-current" : ""}`} />
          </button>
          <button className="tap-hit p-2 rounded-lg hover:bg-raise text-zinc-300 shrink-0" onClick={() => step(-1)} disabled={idle} title="Previous track">
            <SkipBack className="h-4 w-4" />
          </button>
          <button
            className={`tap-hit p-2.5 rounded-lg bg-accent on-accent shrink-0 ${idle ? "opacity-40 pointer-events-none" : ""}`}
            onClick={togglePlay}
            disabled={idle}
            title={playing ? "Pause" : "Play"}
          >
            {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
          </button>
          <button className="tap-hit p-2 rounded-lg hover:bg-raise text-zinc-300 shrink-0" onClick={() => step(1)} disabled={idle} title="Next track">
            <SkipForward className="h-4 w-4" />
          </button>
          <button
            className={`tap-hit p-2 rounded-lg hover:bg-raise text-zinc-400 shrink-0 ${idle ? "opacity-40 pointer-events-none" : ""}`}
            onClick={() => openFullscreen()}
            disabled={idle}
            title="Fullscreen player"
          >
            <Maximize2 className="h-4 w-4" />
          </button>
        </div>

        {/* music-video popout: the REAL decoder (with sound) behind every
            video. It is portaled to <body> and never moves in the DOM — the
            fullscreen player only restyles it to fill the viewport (z-40,
            just under the z-50 overlay), so there is exactly one decoder, one
            network stream, no drift and no re-parenting React could trip
            over when the track changes while the overlay is open. */}
        {isVideo && current && createPortal(
          <div
            className={fullscreen
              ? "fixed inset-0 z-40 bg-black"
              : "fixed right-3 bottom-[5.25rem] z-[15] w-80 max-w-[80vw] rounded-xl overflow-hidden border border-border bg-black shadow-2xl"}
          >
            {!fullscreen && (
              <div className="flex items-center gap-2 px-2.5 py-1.5 bg-zinc-950/90">
                <span className="text-[11px] text-zinc-300 break-words flex-1 min-w-0">{displayTitle}</span>
                <button
                  className="p-1 rounded hover:bg-white/10 text-zinc-400 hover:text-white shrink-0"
                  onClick={() => openFullscreen()}
                  title="Open fullscreen player"
                >
                  <Maximize2 className="h-3.5 w-3.5" />
                </button>
                <button
                  className="p-1 rounded hover:bg-white/10 text-zinc-400 hover:text-white shrink-0"
                  onClick={() => step(1)}
                  title="Skip video"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            )}
            <VideoPopout
              path={current.path}
              videoRef={videoRef}
              fill={fullscreen}
              preferTranscode={preferTranscode}
              aspect={videoAspect}
              captions={captions}
              onTime={onVideoTime}
              onMeta={(e) => {
                // A transcode-fallback remount creates a fresh element with
                // default volume/rate — re-apply the stored ones, and attach
                // it to the analyser (the visible meter is the video's).
                e.currentTarget.volume = vol;
                e.currentTarget.playbackRate = speed;
                attachAnalyser(e.currentTarget);
                onVideoMeta(e);
              }}
              onEnded={handleEnded}
              onPlay={(e) => countPlay(e.currentTarget)}
            />
          </div>,
          document.body
        )}

        {/* portal to <body>: the fullscreen player must escape the right
            column's stacking context (z-10) or the sidebar (z-20) paints
            over it and it stops being truly fullscreen */}
        {fullscreen &&
          current &&
          createPortal(
            <NowPlayingView
              current={current}
              queuePos={`${index + 1}/${queue.length}`}
              playing={!!playing}
              time={time}
              duration={effDuration}
              shuffle={shuffle}
              loop={loop}
              liked={liked}
              onTogglePlay={togglePlay}
              onSeek={(t) => {
                const a = media();
                if (!a) return;
                a.currentTime = t;
                setTime(t);
              }}
              onStep={step}
              onToggleShuffle={() => setShuffle(!shuffle)}
              onToggleLoop={() => setLoop(!loop)}
              onToggleLike={toggleLike}
              onClose={closeFullscreen}
              getAudioTime={getAudioTime}
              speed={speed}
              onSpeedChange={setSpeed}
              video={{
                aspect: videoAspect,
                captions,
                onAspect: (a) => {
                  setVideoAspect(a);
                  writeAspect(a);
                },
                onCaptions: setCaptions,
              }}
              rg={{
                mode: rgMode,
                preamp: rgPreamp,
                applied:
                  rgGain === null
                    ? null
                    : { gain: rgGain, source: rgRes?.source ?? null, analyzed: !!rgRes?.analyzed },
                onMode: (m) => saveRg({ replaygain_mode: m }),
                onPreamp: (db) => saveRg({ replaygain_preamp_db: db }),
              }}
            />,
            document.body
          )}

        {lyricsOpen && (
          <LyricsSidebar
            current={current}
            playing={!!playing}
            time={time}
            onSeek={(t) => {
              const a = media();
              if (!a) return;
              a.currentTime = t;
              setTime(t);
            }}
            getAudioTime={getAudioTime}
            onClose={() => setLyricsOpen(false)}
          />
        )}
      </div>
    </div>
  );
}

/** Popout music-video player: a REAL <video> element (with sound) wired to
 * the player bar's clock, subtitles included. The codec probe picks direct
 * bytes vs live transcode upfront; an onError retry still backs it up. */
function VideoPopout({
  path,
  videoRef,
  fill = false,
  preferTranscode = false,
  aspect = "contain",
  captions = null,
  onTime,
  onMeta,
  onEnded,
  onPlay,
}: {
  path: string;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  /** Draw filling the viewport (fullscreen) instead of the popout card's
   * aspect-ratio box — and drop the native controls, since the viewer draws
   * its own transport over the picture. */
  fill?: boolean;
  preferTranscode?: boolean;
  /** object-fit for the fullscreen picture (contain / cover / stretch). */
  aspect?: VideoAspect;
  /** Caption track to show: null = as tagged (the `default` track), -1 = off,
   * >= 0 = that entry of `tracks`. */
  captions?: number | null;
  onTime: (e: SyntheticEvent<HTMLVideoElement>) => void;
  onMeta: (e: SyntheticEvent<HTMLVideoElement>) => void;
  onEnded: (e?: SyntheticEvent<HTMLVideoElement>) => void;
  /** One playback start, for the play history — see PlayerBar's `countPlay`. */
  onPlay: (e: SyntheticEvent<HTMLMediaElement>) => void;
}) {
  const tracks = useSubtitleTracks(path);
  const trackKey = tracks.map((t) => t.key).join("|");
  const [failed, setFailed] = useState(false);
  const [errorFallback, setErrorFallback] = useState(false);
  // The cached copy of this video: a blob: URL once the lookup has run and the
  // download is there, null when it is not, `undefined` while the lookup is
  // still in flight — then the element gets no src at all, because starting it
  // on the network URL would fire a request that fails offline and trip
  // onError into `failed` a tick before the blob arrives.
  const [cached, setCached] = useState<string | null>();
  useEffect(() => {
    let on = true;
    setCached(undefined);
    void offlineMediaUrl(path).then((url) => {
      if (!on) return;
      if (!url && isOffline()) {
        // Doomed either way, so say so instead of leaving a black rectangle.
        toast.error(`“${path.split(/[\\/]/).pop() ?? path}” isn’t downloaded — it needs the server to play.`);
      }
      setCached(url);
    });
    return () => { on = false; };
  }, [path]);
  useEffect(() => {
    setFailed(false);
    setErrorFallback(false);
  }, [path]);
  // `<track default>` only decides the track's INITIAL mode, so the choice is
  // driven explicitly: one track `showing`, every other `disabled`. Runs on
  // remount too (a track change, a transcode fallback) — re-applying
  // `currentTime`-safe modes is the whole point.
  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    const want = captions === null ? tracks.findIndex((t) => t.default) : captions;
    for (let i = 0; i < el.textTracks.length; i++) {
      el.textTracks[i].mode = i === want ? "showing" : "disabled";
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [captions, trackKey, path, errorFallback, preferTranscode]);
  // The probe decision and the onError fallback both force the live stream;
  // derived, so a late-arriving probe result needs no state syncing.
  const live = errorFallback || preferTranscode;
  // A cached copy holds the DIRECT stream's bytes, so the transcode decision
  // normally wins over it — but with the server away the cached copy is the
  // only thing that can play, so offline it wins instead. A blob: URL goes in
  // as-is: the query params the callers below assume (`?transcode=1`, the
  // token) describe a network request, and appending one to a blob would be a
  // URL for a different resource than the bytes we already have.
  const cachedSrc = cached && (!live || isOffline()) ? cached : null;
  const src = cachedSrc ?? (cached === undefined && !live ? undefined : api.videoStreamUrl(path, live));
  if (failed) {
    return (
      <div className="flex items-center justify-center p-6 text-center text-[11px] text-zinc-400">
        This video can't play in the browser. Remux it (album page → Remux videos) or open the file externally.
      </div>
    );
  }
  return (
    <video
      key={`${path}|${live ? "x" : "direct"}`}
      ref={videoRef}
      src={src}
      controls={!fill}
      autoPlay
      playsInline
      preload="auto"
      onTimeUpdate={onTime}
      onLoadedMetadata={onMeta}
      onEnded={onEnded}
      onPlay={onPlay}
      onError={() => {
        // Direct bytes failed (MPEG-2/VC-1/etc.) — retry via live transcode.
        if (!live) setErrorFallback(true);
        else setFailed(true);
      }}
      className={fill ? `h-full w-full ${ASPECT_FIT[aspect]} bg-black` : "w-full aspect-video bg-black"}
    >
      {tracks.map((t) => (
        <track key={t.key} kind="subtitles" src={t.src} label={t.label} default={t.default} />
      ))}
    </video>
  );
}
