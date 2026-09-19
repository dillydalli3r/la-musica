import { useState } from "react";
import { Loader2, Upload } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";

/** Strip timestamps ([mm:ss.xx] line stamps and <mm:ss.xx> word stamps) so
 * a synced paste also fills LRCLIB's required plain text field. */
function toPlain(text: string): string {
  return text
    .split(/\r?\n/)
    .map((l) =>
      l
        .replace(/\[\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?\]/g, "")
        .replace(/<\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?>/g, "")
        .trim()
    )
    .filter(Boolean)
    .join("\n");
}

/** Publish lyrics to LRCLIB (the community database the app fetches from).
 * The metadata starts from the track's own tags; publishing is a public,
 * outward-facing action, so the button confirms in two steps. */
export default function LrclibPublishPanel({
  artist,
  track,
  album,
  duration,
  text,
  onDone,
}: {
  artist: string;
  track: string;
  album?: string;
  duration?: number;
  /** The exact editor text to submit (plain or synced — auto-detected). */
  text: string;
  /** Called after a successful publish. */
  onDone?: () => void;
}) {
  const [a, setA] = useState(artist);
  const [t, setT] = useState(track);
  const [al, setAl] = useState(album ?? "");
  const [dur, setDur] = useState(String(Math.round(duration ?? 0) || ""));
  const [busy, setBusy] = useState(false);
  const [arm, setArm] = useState(false);
  /** The server's "the database already has this recording" answer, kept so the
   *  panel can offer the override instead of dead-ending. Publishing is public
   *  and outward-facing, so the override is a separate, explicitly-labelled
   *  press — never something one click does by accident. */
  const [exists, setExists] = useState(false);

  const trimmed = text.trim();
  const synced = /\[\d{1,2}:\d{1,2}/.test(trimmed);
  const lines = trimmed ? trimmed.split(/\r?\n/).length : 0;

  if (!trimmed) {
    return (
      <div className="text-[11px] text-zinc-500">
        Nothing to publish yet — the lyrics are empty.
      </div>
    );
  }

  const publish = async (force = false) => {
    if (!a.trim() || !t.trim()) {
      toast("Artist and track name are required to publish");
      return;
    }
    // Two steps for a first publish; the override button IS the second step
    // for a force-submit (it appears only after a refusal and says what it
    // does), so it must not silently become a three-click act.
    if (!arm && !force) {
      setArm(true);
      setExists(false);
      setTimeout(() => setArm(false), 4000);
      return;
    }
    setBusy(true);
    try {
      const r = await api.lyricsPublish({
        artist: a.trim(),
        track: t.trim(),
        album: al.trim() || undefined,
        duration: Number(dur) || undefined,
        synced: synced ? trimmed : undefined,
        plain: synced ? toPlain(trimmed) : trimmed,
        force: force || undefined,
      });
      toast(r.message ?? (r.ok ? "Published to LRCLIB" : "Publish failed"));
      if (r.ok) {
        setArm(false);
        setExists(false);
        onDone?.();
      } else if (r.exists) {
        setExists(true);
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <label className="text-[10px] text-zinc-500 block">
          Artist
          <input className="input !py-1 !px-2 text-xs mt-0.5 w-full" value={a} onChange={(e) => setA(e.target.value)} />
        </label>
        <label className="text-[10px] text-zinc-500 block">
          Track
          <input className="input !py-1 !px-2 text-xs mt-0.5 w-full" value={t} onChange={(e) => setT(e.target.value)} />
        </label>
        <label className="text-[10px] text-zinc-500 block">
          Album
          <input className="input !py-1 !px-2 text-xs mt-0.5 w-full" value={al} onChange={(e) => setAl(e.target.value)} />
        </label>
        <label className="text-[10px] text-zinc-500 block">
          Duration (s)
          <input
            className="input !py-1 !px-2 text-xs mt-0.5 w-full"
            value={dur}
            inputMode="numeric"
            onChange={(e) => setDur(e.target.value.replace(/[^\d]/g, ""))}
          />
        </label>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button
          className={`btn-primary !py-1 text-xs ${arm ? "!bg-red-600" : ""}`}
          disabled={busy}
          onClick={() => publish(false)}
          title={synced ? "Publishes the synced (timestamped) lyrics" : "Publishes as plain (untimed) lyrics"}
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
          {arm ? "Confirm — this is public" : "Publish to LRCLIB"}
        </button>
        <span className="text-[10px] text-zinc-600">
          {lines} line{lines === 1 ? "" : "s"} · {synced ? "synced" : "plain"} · goes live publicly on lrclib.net
        </span>
      </div>
      {exists && (
        /* LRCLIB already answers for this recording. The app does not replace
           a community copy on its own, but a person correcting their own
           submission must be able to — so the override is offered, labelled
           with what it does, and sent only from here. */
        <div className="rounded-md border border-amber-900/50 bg-amber-950/20 px-2 py-1.5 space-y-1">
          <div className="text-[11px] text-amber-200/90">
            LRCLIB already holds lyrics for this recording. Submitting anyway adds
            or replaces the entry for it.
          </div>
          <button
            className="btn-ghost !py-1 text-xs"
            disabled={busy}
            onClick={() => publish(true)}
            title="Submit despite the existing LRCLIB entry"
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
            Submit anyway
          </button>
        </div>
      )}
    </div>
  );
}
