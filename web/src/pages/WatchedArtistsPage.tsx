import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  BellRing, ChevronDown, ChevronRight, ExternalLink, Loader2, Pause, Pencil, Play,
  RefreshCw, Trash2,
} from "lucide-react";
import { api } from "../api";
import type { Watch, WatchItem } from "../api";
import PageHeader from "../components/PageHeader";
import { EmptyState, PageLoading } from "../components/Badges";
import ConfirmButton from "../components/ConfirmButton";
import WatchDialog, { useWatches, watchTimeAgo, watchTimeFrom, typeSummary } from "../components/WatchDialog";
import { toast } from "../store";
import { artistMbid } from "../lib/refs";

/** An artist id, or a MusicBrainz link pasted from a browser tab. Both are
 *  accepted because both are what a user has to hand; anything else is
 *  rejected here rather than sent to the server to fail there. */
const MB_ARTIST_URL_RE = /musicbrainz\.org\/artist\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
const MBID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** What a watch did to one release group, as a chip. */
const ITEM_CHIP: Record<WatchItem["status"], { label: string; cls: string }> = {
  queued: { label: "Queued", cls: "bg-sky-950/50 text-sky-300 border border-sky-900/60" },
  notified: { label: "Notified", cls: "bg-amber-950/40 text-amber-300/90 border border-amber-900/50" },
  imported: { label: "Imported", cls: "bg-emerald-950/40 text-emerald-300/90 border border-emerald-900/50" },
  failed: { label: "Failed", cls: "bg-red-950/50 text-red-300 border border-red-900/60" },
};

/** One watch: what it will fetch, when it last ran and when it runs next, what
 *  it did, and the four things a user can do to it. */
function WatchRow({ w, onEdit, onPick }: { w: Watch; onEdit: () => void; onPick: () => void }) {
  const qc = useQueryClient();
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  const refresh = () => qc.invalidateQueries({ queryKey: ["watches"] });

  /** The server's own sentence about this run is the result the user sees:
   *  "queued 1 of 12" and "nothing new since <date>" mean opposite things. */
  const checkNow = async () => {
    setChecking(true);
    try {
      const r = await api.watchCheck(w.id);
      toast.success(r.summary || `Checked ${w.artist} — nothing new`);
      refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  };

  const setEnabled = async (enabled: boolean) => {
    setSaving(true);
    try {
      await api.watchUpdate(w.id, { enabled });
      toast(enabled ? `Watching ${w.artist} again` : `Paused ${w.artist}`);
      refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    setSaving(true);
    try {
      await api.watchDelete(w.id);
      toast(`Stopped watching ${w.artist}`);
      refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const items = [...(w.queued ?? []), ...(w.imported ?? [])].sort((a, b) => b.at - a.at);
  const busy = checking || saving;

  return (
    <li className="panel space-y-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`chip border ${w.enabled
          ? "bg-accent/15 border-accent/30 text-accent"
          : "bg-white/5 border-white/15 text-zinc-400"}`}>
          {w.enabled ? <BellRing className="h-3 w-3" /> : <Pause className="h-3 w-3" />}
          {w.enabled ? "Watching" : "Paused"}
        </span>
        <span className="text-sm font-medium text-zinc-200 min-w-0 truncate">{w.artist}</span>
        <a
          className="text-zinc-500 hover:text-accent-soft"
          href={`https://musicbrainz.org/artist/${w.artist_mbid}`}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open ${w.artist} on MusicBrainz`}
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </a>

        <div className="ml-auto flex items-center gap-1.5">
          <button className="btn-ghost !py-1.5 text-xs" onClick={checkNow} disabled={busy}
            title="Browse MusicBrainz now and queue what the rules allow (at most the per-cycle limit)">
            {checking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            {checking ? "Checking…" : "Check now"}
          </button>
          <button className="btn-ghost !py-1.5 text-xs" onClick={onPick} disabled={busy}
            title="Choose which of this artist's release groups the watch may fetch">
            Choose releases…
          </button>
          <button className="btn-ghost !py-1.5 text-xs" onClick={onEdit} disabled={busy} title="Edit this watch's rules">
            <Pencil className="h-3.5 w-3.5" /> Edit
          </button>
          <button className="btn-ghost !py-1.5 text-xs" onClick={() => setEnabled(!w.enabled)} disabled={busy}
            title={w.enabled ? "Stop checking this artist (the watch and its history stay)" : "Start checking again"}>
            {w.enabled ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            {w.enabled ? "Pause" : "Resume"}
          </button>
          <ConfirmButton
            iconOnly
            className="btn-icon"
            onConfirm={remove}
            disabled={busy}
            title={`Remove the watch on ${w.artist} (nothing already queued or imported is touched)`}
            confirmLabel="Remove watch"
          >
            <Trash2 className="h-4 w-4" />
          </ConfirmButton>
        </div>
      </div>

      <div className="flex items-center gap-2 flex-wrap text-[11px]">
        <span className="chip bg-raise border border-border text-zinc-300" title={w.policy === "backfill"
          ? "Backfill: it walks what is already out, max per cycle at a time"
          : "New releases only: nothing that was already out when the watch started"}>
          {w.policy === "backfill" ? "Backfill what is missing" : "New releases only"}
        </span>
        <span className="chip bg-raise border border-border text-zinc-300" title="Only release groups of these types are fetched">
          {typeSummary(w.release_types)}
        </span>
        <span className="chip bg-raise border border-border text-zinc-300" title="The ceiling per check — never more than this many release groups in one cycle">
          {w.max_per_cycle} per check
        </span>
        <span className={`chip border ${w.auto_add
          ? "bg-emerald-950/30 border-emerald-900/50 text-emerald-300/90"
          : "bg-amber-950/30 border-amber-900/50 text-amber-300/90"}`}
          title={w.auto_add
            ? "The release is queued straight into the library and the usual search-and-import runs"
            : "Notify only: nothing is fetched until you ask for it from the wish list"}>
          {w.auto_add ? "Auto-add" : "Notify only"}
        </span>
        {(w.include?.length ?? 0) > 0 && (
          <span className="chip bg-raise border border-border text-zinc-300" title="Only these release groups may ever be fetched">
            {w.include.length} chosen only
          </span>
        )}
        {w.exclude?.length > 0 && (
          <span className="chip bg-red-950/40 text-red-300/90 border border-red-900/50" title="These release groups are never fetched">
            {w.exclude.length} blocked
          </span>
        )}
      </div>

      <div className="text-[11px] text-zinc-500 space-y-0.5">
        <div>
          last check {watchTimeAgo(w.last_checked_at)}
          {w.last_checked_at ? ` (${w.checked_count} total)` : ""}
          {" · "}
          {!w.enabled
            ? "paused — no check is scheduled"
            : w.next_check_at
            ? `next ${watchTimeFrom(w.next_check_at)}`
            : "nothing scheduled yet — the watcher picks it up on its next run"}
        </div>
        <div>
          since then: {w.queued_count} queued · {w.imported_count} imported · {w.notified_count} notified
        </div>
        {w.last_result && <div title="What the server's last check on this artist did">{w.last_result}</div>}
        {w.last_error && <div className="text-red-300/90" title="The last check's error">{w.last_error}</div>}
      </div>

      {items.length > 0 && (
        <>
          <button className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-zinc-500 hover:text-white"
            onClick={() => setOpen((v) => !v)} aria-expanded={open}>
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            What it picked up · {items.length}
          </button>
          {open && (
            <ul className="space-y-1">
              {items.map((it) => {
                const chip = ITEM_CHIP[it.status] ?? ITEM_CHIP.queued;
                return (
                  <li key={`${it.status}-${it.release_group_mbid}-${it.at}`} className="flex items-center gap-2 text-[11px]">
                    <span className={`chip ${chip.cls}`}>{chip.label}</span>
                    <span className="text-zinc-300 min-w-0 truncate" title={it.title}>{it.title || it.release_group_mbid}</span>
                    <span className="text-zinc-600">{it.year}</span>
                    <span className="text-zinc-600 ml-auto shrink-0" title={new Date(it.at * 1000).toLocaleString()}>
                      {watchTimeAgo(it.at)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

/** "Add artist": a MusicBrainz id or link, or one of the library artists whose
 *  MusicBrainz id the tags already carry — the two ways a user arrives at the
 *  question. Both open the same dialog, so the rules are picked in one place. */
function AddArtistPanel({ watched, onPick }: {
  watched: Set<string>;
  onPick: (artistMbid: string, artist: string) => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const lib = useQuery({ queryKey: ["library"], queryFn: () => api.library() });

  // The library's own artists, deduplicated by MusicBrainz id: two folders of
  // the same artist are one artist to watch.
  const linked = new Map<string, string>();
  for (const a of lib.data?.artists ?? []) {
    const mbid = artistMbid(a);
    if (mbid && !watched.has(mbid) && !linked.has(mbid)) linked.set(mbid, a.display_name || a.name);
  }
  const rows = [...linked.entries()].sort((a, b) => a[1].localeCompare(b[1])).slice(0, 60);

  const submit = () => {
    const typed = text.trim();
    const mbid = (MB_ARTIST_URL_RE.exec(typed)?.[1] ?? MBID_RE.exec(typed)?.[0] ?? "").toLowerCase();
    if (!mbid) {
      setError("Paste a MusicBrainz artist id or an artist link (musicbrainz.org/artist/…).");
      return;
    }
    if (watched.has(mbid)) {
      setError("That artist is already watched — edit the watch in the list below.");
      return;
    }
    setError("");
    // The name is unknown until the server resolves the id; the dialog and the
    // list row both show what the server reports afterwards.
    onPick(mbid, linked.get(mbid) ?? "");
  };

  return (
    <div className="panel space-y-3">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">Add an artist</div>
      <div className="flex items-start gap-2 flex-wrap">
        <input
          className="input !py-1.5 text-xs flex-1 min-w-[240px]"
          placeholder="MusicBrainz artist id or https://musicbrainz.org/artist/…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
        />
        <button className="btn-primary !py-1.5 text-xs" onClick={submit}>Watch</button>
      </div>
      {error && <div className="text-[11px] text-amber-300/90" role="alert">{error}</div>}
      {rows.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-600 pb-1">
            Or one of your artists with a MusicBrainz id
          </div>
          <div className="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto">
            {rows.map(([mbid, name]) => (
              <button
                key={mbid}
                className="chip bg-raise border border-border text-zinc-300 hover:text-white"
                title={`Watch ${name} for new releases`}
                onClick={() => onPick(mbid, name)}
              >
                {name}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Sidebar MAINTAIN → Watched artists: every artist the app keeps an eye on,
 *  from GET /api/watches (server/api_watch.py).
 *
 *  A watch is deliberately NOT "get this artist's discography". It browses the
 *  artist's release groups, keeps only the ones the rules allow (types, the
 *  allow/block lists), and queues at most `max_per_cycle` of them per check;
 *  `new_only` — the default — ignores everything that was already out when the
 *  watch was added. This page is where that is visible: what each watch would
 *  fetch, when it next runs, and what the last run actually did. */
export default function WatchedArtistsPage() {
  const { data, isLoading, error } = useWatches();
  const [dialog, setDialog] = useState<{ mbid: string; name: string; watch?: Watch; picker: boolean } | null>(null);

  const watches = data?.watches ?? [];
  const worker = data?.worker;
  const watchedMbids = new Set(watches.map((w) => w.artist_mbid));
  const paused = watches.filter((w) => !w.enabled).length;

  return (
    <div className="p-6 space-y-5 mx-auto max-w-5xl">
      <PageHeader
        icon={BellRing}
        title="Watched artists"
        subtitle="Artists the app checks for new releases. A watch queues a few release groups per check into the wish queue — never a whole discography."
        chips={[
          `${watches.length} watched`,
          ...(paused ? [`${paused} paused`] : []),
          ...(worker
            ? [worker.running
                ? `checking${worker.current?.length ? ` ${worker.current.join(", ")}` : ""}`
                : worker.next_run ? `worker idle · next ${watchTimeFrom(worker.next_run)}` : "worker idle"]
            : []),
        ]}
      />

      {worker && (
        <div className="text-[11px] text-zinc-500">
          {worker.running
            ? "The watcher is working through the list now"
            : worker.next_run
            ? `The watcher next runs ${watchTimeFrom(worker.next_run)}`
            : "The watcher has nothing scheduled — a watch is due at its next tick"}
          {worker.cycles ? ` · ${worker.cycles} cycles since the server started` : ""}
          {worker.last_result ? ` · last cycle: ${worker.last_result}` : ""}
        </div>
      )}

      <AddArtistPanel
        watched={watchedMbids}
        onPick={(mbid, name) => setDialog({ mbid, name, picker: false })}
      />

      {error ? (
        <EmptyState
          title="Could not read the watch list"
          hint={error instanceof Error ? error.message : String(error)}
        />
      ) : isLoading ? (
        <PageLoading label="Reading your watches…" />
      ) : watches.length === 0 ? (
        <EmptyState
          title="No artists watched yet"
          hint="A watch picks up only releases NEWER than the moment you add it — never what is already out — unless you choose Backfill, which walks the existing discography. Either way it queues at most the per-cycle limit (1 by default) each time it checks, so a discography trickles in instead of arriving at once."
        />
      ) : (
        <ul className="space-y-3">
          {watches.map((w) => (
            <WatchRow
              key={w.id}
              w={w}
              onEdit={() => setDialog({ mbid: w.artist_mbid, name: w.artist, watch: w, picker: false })}
              onPick={() => setDialog({ mbid: w.artist_mbid, name: w.artist, watch: w, picker: true })}
            />
          ))}
        </ul>
      )}

      <div className="text-[11px] text-zinc-600">
        Releases are found by browsing MusicBrainz once per artist per check, then handed to the normal wish
        queue — see <Link className="text-accent-soft hover:underline" to="/soulseek">Soulseek</Link> for what
        each wish is doing, and Settings → Artist watch for the worker's interval.
      </div>

      {dialog && (
        <WatchDialog
          artistMbid={dialog.mbid}
          artist={dialog.name}
          watch={dialog.watch}
          focusReleases={dialog.picker}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}
