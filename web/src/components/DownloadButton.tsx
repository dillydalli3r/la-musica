import { useEffect, useState } from "react";
import { CheckCircle2, CircleDashed, Download, Loader2 } from "lucide-react";
import ConfirmButton from "./ConfirmButton";
import { cacheTrack, isTrackCached, uncacheTrack } from "../lib/mediaCache";
import { toast } from "../store";

/** How many of `paths` are already in the offline cache. */
async function countCached(paths: string[]): Promise<number> {
  const hits = await Promise.all(paths.map(isTrackCached));
  return hits.filter(Boolean).length;
}

/** "Downloaded" means the audio is in the browser's offline cache (see
 *  lib/mediaCache) — one control for a whole entity: an album, an artist's
 *  catalogue, a playlist, or a single track.
 *
 *  Three states: nothing cached, some cached (the button finishes the job, it
 *  never claims a full download), all cached — where pressing again REMOVES
 *  them. A bulk removal arms first (ConfirmButton); a single track toggles
 *  directly, the way the player bar's own download button already does. */
export default function DownloadButton({
  paths,
  label = "Download",
  size = "sm",
}: {
  /** The entity's track paths — `track.path` as the API reports it. */
  paths: string[];
  /** Text of the idle state; the cached/partial states name themselves. */
  label?: string;
  size?: "sm" | "md";
}) {
  const [state, setState] = useState<"none" | "partial" | "full">("none");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(0);
  // Callers build the list inline, so a fresh array arrives every render —
  // the joined content is what actually changes.
  const key = paths.join("\n");

  useEffect(() => {
    let dead = false;
    setState("none");
    if (!paths.length) return;
    countCached(paths).then((n) => {
      if (dead) return;
      setState(n === 0 ? "none" : n === paths.length ? "full" : "partial");
    });
    return () => {
      dead = true;
    };
  }, [key]);

  const rescan = async () => {
    const n = paths.length ? await countCached(paths) : 0;
    setState(n === 0 ? "none" : n === paths.length ? "full" : "partial");
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

  const cls = size === "sm" ? "btn-ghost !py-1.5 text-xs" : "btn-ghost";
  const iconCls = size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4";
  const count = `${paths.length} track${paths.length === 1 ? "" : "s"}`;

  if (state === "full") {
    const body = (
      <>
        <CheckCircle2 className={`${iconCls} text-emerald-500`} /> Downloaded
      </>
    );
    // Bulk removal is worth a second click; one track is not.
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
      <button
        className={cls}
        onClick={remove}
        disabled={busy}
        title="Downloaded for offline playback — click to remove"
      >
        {body}
      </button>
    );
  }

  return (
    <button
      className={cls}
      onClick={download}
      disabled={busy || !paths.length}
      title={
        state === "partial"
          ? `Partly downloaded — click to cache the remaining of ${count}`
          : `Download ${count} for offline playback`
      }
    >
      {busy ? (
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
