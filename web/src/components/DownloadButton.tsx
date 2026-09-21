import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, Download, X } from "lucide-react";
import ConfirmButton from "./ConfirmButton";
import {
  CACHED_PATHS_KEY,
  cachedFlags,
  cancelDownloads,
  downloadTracks,
  uncacheTrack,
  type DownloadProgress,
} from "../lib/mediaCache";
import { toast } from "../store";

/** How many of `paths` are already in the offline cache. */
async function countCached(paths: string[]): Promise<number> {
  return (await cachedFlags(paths)).filter(Boolean).length;
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
 *  A run in flight is a FOURTH state: the same control becomes the cancel and
 *  reports "done/total" (an arc, icon-only) as the bounded queue works through
 *  the selection. Downloads of every kind go through lib/mediaCache's queue —
 *  N tracks at a time, one bulk request per chunk — and a failure comes back
 *  with the reason for the file it belongs to.
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
  emptyReason = "Nothing to download — no tracks here",
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
  /** Why the button is inert with an empty selection — a page whose own
   *  selection can be empty says so instead of "Download 0 tracks". */
  emptyReason?: string;
}) {
  const qc = useQueryClient();
  const [state, setState] = useState<"none" | "partial" | "full">("none");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(0);
  const [have, setHave] = useState(0);
  // What the running queue has finished and which track it is on, live: the
  // run is the button's own, so it reports as it goes rather than at the end.
  const [prog, setProg] = useState<DownloadProgress | null>(null);
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
    setProg(null);
    try {
      const hits = await cachedFlags(paths);
      const todo = paths.filter((_, i) => !hits[i]);
      if (!todo.length) {
        toast("Already downloaded");
        return;
      }
      // One bounded, cancellable run: N tracks at a time (server config
      // `download_concurrency`), with a bulk request per chunk when the
      // server offers one. Nothing here fires a promise per track.
      const report = await downloadTracks(todo, {
        onProgress: (p) => {
          setProg(p);
          setDone(p.done);
        },
      });
      if (report.failures.length) {
        // One dead file must not abandon the rest — but a bare count ("2 of
        // 2 could not be downloaded") leaves nothing to act on, so the
        // reason travels out with the file it belongs to.
        const rows = report.failures.map((f) => {
          const name = f.path ? f.path.split(/[\\/]/).pop() || f.path : "";
          return name ? `${name} — ${f.message}` : f.message;
        });
        const head = rows.slice(0, 2).join(" · ");
        toast.error(
          `${report.failures.length} of ${todo.length} track(s) could not be downloaded: ${head}` +
            (rows.length > 2 ? ` · +${rows.length - 2} more` : "")
        );
      } else if (report.cancelled) {
        toast(`Download stopped — ${report.done} of ${todo.length} track(s) are cached`);
      } else {
        toast.success(
          `Downloaded ${report.done} track${report.done === 1 ? "" : "s"} for offline playback`
        );
      }
    } catch (e) {
      // downloadTracks reports per-track failures itself; this is the run
      // failing outright (no Cache Storage, or no answer from the server).
      toast.error(`Download failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
      setProg(null);
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

  if (busy) {
    // While it works, the button IS the cancel: a queue can be a whole
    // discography, and the run has to be stoppable from where it started.
    const current = prog?.current ? prog.current.split(/[\\/]/).pop() : null;
    const stop = current ? `Stop — downloading ${current} (${done}/${paths.length})` : `Stop (${done}/${paths.length})`;
    return (
      <button className={cls} onClick={cancelDownloads} aria-label={stop} title={stop}>
        {iconOnly ? (
          <span className="icon-swap inline-flex">
            <ProgressRing pct={pct} className="text-accent" />
          </span>
        ) : (
          <>
            <X className={iconCls} /> {done}/{paths.length}
          </>
        )}
      </button>
    );
  }

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

  /// The state, as one glyph: the arc amber when part of the entity is already
  /// cached (the busy ring is the cancel button above), the arrow when there is
  /// nothing yet. Each swap scales in rather than cutting.
  const glyph = state === "partial" ? (
    <ProgressRing pct={pct} className="text-amber-400" />
  ) : (
    <Download className={iconCls} />
  );

  return (
    <button
      className={cls}
      onClick={download}
      disabled={!paths.length}
      aria-label={iconOnly
        ? (paths.length
            ? `${state === "partial" ? "Partly downloaded — " : ""}Download ${count} for offline playback`
            : emptyReason)
        : undefined}
      title={
        !paths.length
          ? emptyReason
          : state === "partial"
            ? `Partly downloaded — click to cache the remaining of ${count}`
            : `Download ${count} for offline playback`
      }
    >
      {iconOnly ? (
        <span key={state} className="icon-swap inline-flex">
          {glyph}
        </span>
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
