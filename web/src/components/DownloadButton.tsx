import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, Download, Loader2 } from "lucide-react";
import ConfirmButton from "./ConfirmButton";
import { CACHED_PATHS_KEY, cacheTrack, isTrackCached, uncacheTrack } from "../lib/mediaCache";
import { toast } from "../store";

/** How many of `paths` are already in the offline cache. */
async function countCached(paths: string[]): Promise<number> {
  const hits = await Promise.all(paths.map(isTrackCached));
  return hits.filter(Boolean).length;
}

/** Determinate progress ring — the square button's stand-in for the "3/8" the
 *  labelled variant prints. The arc is the fraction cached, so a bulk download
 *  is visibly filling instead of just spinning. */
function ProgressRing({ pct, className = "" }: { pct: number; className?: string }) {
  const r = 6.5;
  const circumference = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 16 16" className={`h-4 w-4 -rotate-90 ${className}`} aria-hidden>
      <circle cx="8" cy="8" r={r} fill="none" stroke="currentColor" strokeWidth="2" opacity="0.25" />
      <circle
        cx="8"
        cy="8"
        r={r}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - Math.max(0.02, Math.min(1, pct)))}
        style={{ transition: "stroke-dashoffset 0.25s ease-out" }}
      />
    </svg>
  );
}

/** "Downloaded" means the audio is in the browser's offline cache (see
 *  lib/mediaCache) — one control for a whole entity: an album, an artist's
 *  catalogue, a playlist, or a single track.
 *
 *  Three states: nothing cached, some cached (the button finishes the job, it
 *  never claims a full download), all cached — where pressing again REMOVES
 *  them. A bulk removal arms first (ConfirmButton); a single track toggles
 *  directly, the way the player bar's own download button already does.
 *
 *  `iconOnly` is the square button the album/playlist action row uses: the
 *  state has to read from the glyph alone there, so it NEVER prints a label —
 *  a progress arc fills while it caches, the check scales in when it is done,
 *  and removing arms the same box red instead of expanding it. */
export default function DownloadButton({
  paths,
  label = "Download",
  size = "sm",
  iconOnly = false,
}: {
  /** The entity's track paths — `track.path` as the API reports it. */
  paths: string[];
  /** Text of the idle state; the cached/partial states name themselves. */
  label?: string;
  size?: "sm" | "md";
  /** A square icon button with no text, for an action row of icon buttons
   *  (the album page). Everything the label carried moves into the tooltip
   *  and the aria-label. */
  iconOnly?: boolean;
}) {
  const qc = useQueryClient();
  const [state, setState] = useState<"none" | "partial" | "full">("none");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(0);
  const [have, setHave] = useState(0);
  // Callers build the list inline, so a fresh array arrives every render —
  // the joined content is what actually changes.
  const key = paths.join("\n");

  useEffect(() => {
    let dead = false;
    setState("none");
    setHave(0);
    if (!paths.length) return;
    countCached(paths).then((n) => {
      if (dead) return;
      setHave(n);
      setState(n === 0 ? "none" : n === paths.length ? "full" : "partial");
    });
    return () => {
      dead = true;
    };
  }, [key]);

  /** Re-read this button's own state AND tell the rest of the app: every
   *  downloaded mark on a track title reads the shared snapshot, and the
   *  downloads page reads its byte total — both are stale the moment the
   *  cache changes here. */
  const rescan = async () => {
    const n = paths.length ? await countCached(paths) : 0;
    setHave(n);
    setState(n === 0 ? "none" : n === paths.length ? "full" : "partial");
    qc.invalidateQueries({ queryKey: CACHED_PATHS_KEY });
    qc.invalidateQueries({ queryKey: ["cachedBytes"] });
  };

  const download = async () => {
    setBusy(true);
    setDone(0);
    try {
      const hits = await Promise.all(paths.map(isTrackCached));
      const todo = paths.filter((_, i) => !hits[i]);
      if (!todo.length) {
        toast("Already downloaded");
        return;
      }
      let n = 0;
      let failed = 0;
      for (const p of todo) {
        try {
          await cacheTrack(p);
        } catch {
          failed += 1; // one dead file must not abandon the rest
        }
        setDone(++n);
      }
      if (failed) toast.error(`${failed} of ${todo.length} track(s) could not be downloaded`);
      else toast.success(`Downloaded ${todo.length} track${todo.length === 1 ? "" : "s"} for offline playback`);
    } finally {
      setBusy(false);
      await rescan();
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await Promise.all(paths.map(uncacheTrack));
      toast.success(`Removed ${paths.length} track${paths.length === 1 ? "" : "s"} from the offline cache`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      await rescan();
    }
  };

  const cls = iconOnly
    ? "btn-icon"
    : size === "sm"
      ? "btn-ghost !py-1.5 text-xs"
      : "btn-ghost";
  const iconCls = iconOnly || size === "md" ? "h-4 w-4" : "h-3.5 w-3.5";
  const count = `${paths.length} track${paths.length === 1 ? "" : "s"}`;
  /** Fraction of the entity already on disk: what is cached plus what this
   *  run has finished (the rescan that updates `have` only lands at the end). */
  const pct = paths.length ? Math.min(1, (have + done) / paths.length) : 0;

  if (state === "full") {
    const tick = <CheckCircle2 className={`${iconCls} text-emerald-500`} />;
    // Bulk removal is worth a second click; one track is not. Icon-only
    // buttons arm IN PLACE (red + pulsing square) instead of growing a
    // label — nothing in the row changes size or shape.
    if (iconOnly) {
      return paths.length > 1 ? (
        <ConfirmButton
          iconOnly
          className={cls}
          onConfirm={remove}
          disabled={busy}
          title={`All ${count} downloaded — click twice to remove them`}
        >
          <span className="icon-swap inline-flex">{tick}</span>
        </ConfirmButton>
      ) : (
        <button
          className={cls}
          onClick={remove}
          disabled={busy}
          aria-label="Downloaded — remove from the offline cache"
          title="Downloaded for offline playback — click to remove"
        >
          <span className="icon-swap inline-flex">{tick}</span>
        </button>
      );
    }
    const body = (
      <>
        {tick} Downloaded
      </>
    );
    return paths.length > 1 ? (
      <ConfirmButton
        className={cls}
        onConfirm={remove}
        disabled={busy}
        confirmLabel={`Remove ${paths.length}?`}
        title={`All ${count} cached for offline playback — click to remove them`}
      >
        {body}
      </ConfirmButton>
    ) : (
      <button className={cls} onClick={remove} disabled={busy} title="Downloaded for offline playback — click to remove">
        {body}
      </button>
    );
  }

  /// The state, as one glyph: progress arc while caching (determinate — the
  /// button has no room for the "3/8" the labelled variant prints), the same
  /// arc amber when part of the entity is already cached, the arrow when
  /// there is nothing yet. Each swap scales in rather than cutting.
  const glyph = busy ? (
    <ProgressRing pct={pct} className="text-accent" />
  ) : state === "partial" ? (
    <ProgressRing pct={pct} className="text-amber-400" />
  ) : (
    <Download className={iconCls} />
  );

  return (
    <button
      className={cls}
      onClick={download}
      disabled={busy || !paths.length}
      aria-label={iconOnly ? `${state === "partial" ? "Partly downloaded — " : ""}Download ${count} for offline playback` : undefined}
      title={
        state === "partial"
          ? `Partly downloaded — click to cache the remaining of ${count}`
          : `Download ${count} for offline playback`
      }
    >
      {iconOnly ? (
        <span key={busy ? "busy" : state} className="icon-swap inline-flex">
          {glyph}
        </span>
      ) : busy ? (
        <>
          <Loader2 className={`${iconCls} animate-spin`} /> {done}/{paths.length}
        </>
      ) : state === "partial" ? (
        <>
          <CircleDashed className={`${iconCls} text-amber-400`} /> Partial
        </>
      ) : (
        <>
          <Download className={iconCls} /> {label}
        </>
      )}
    </button>
  );
}
