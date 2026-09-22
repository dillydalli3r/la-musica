import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { SlidersHorizontal } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import Popover from "./Popover";

/** The credential a source needs, in the words a person uses.
 *
 *  The server names the CONFIG KEY it reads (`rym_cookie`, `discogs_token`),
 *  which is right for the request builder and wrong to show: the tray says
 *  which login is missing, and a key with no entry here falls back to its own
 *  name with the underscores opened up. */
const NEED_LABEL: Record<string, string> = {
  rym_cookie: "RYM cookie",
  lastfm_api_key: "Last.fm API key",
  discogs_token: "Discogs token",
  spotify_client_id: "Spotify client id",
  spotify_client_secret: "Spotify client secret",
};

/** The credential chip's text: what to paste, not the config key. */
const needsText = (keys: string[], join: string) =>
  keys.map((k) => NEED_LABEL[k] ?? k.replace(/_/g, " ")).join(join);

/** The genre sources of one import — which providers the Import genres button
 *  may ask, and in what order.
 *
 *  The rows ARE the chain: they come from `GET /api/sources/health?kind=genre`
 *  in registry order, and a toggle saves `genre_sources` in that SAME order,
 *  so the list the user reads top-to-bottom is the priority order the server
 *  asks. The saved config value is the only state — a tick is a membership
 *  test against it, never a local copy that could drift from what the chain
 *  actually uses. */
export default function GenreSourcesTray({
  align = "left",
  buttonClass = "btn-ghost tap !px-2.5",
}: {
  align?: "left" | "right";
  buttonClass?: string;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const { data: health } = useQuery({
    queryKey: ["sourcesHealth", "genre"],
    queryFn: () => api.sourcesHealth(false, "genre"),
    staleTime: 60_000,
  });
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });

  const rows = health?.sources ?? [];
  const saved = Array.isArray(config?.["genre_sources"])
    ? (config!["genre_sources"] as unknown[]).map(String)
    : [];
  // An empty saved list is the server's own "use the shipped default" (every
  // source), so it reads as everything ticked rather than as nothing.
  const ticked = (id: string) => (saved.length ? saved.includes(id) : true);
  const tickedCount = rows.filter((r) => ticked(r.id)).length;
  const all = rows.map((r) => r.id);

  /** Write the list, then re-read the config it came from. */
  const save = async (ids: string[]) => {
    setSaving(true);
    try {
      await api.saveConfig({ genre_sources: ids });
      await qc.invalidateQueries({ queryKey: ["config"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setSaving(false);
    }
  };

  /** Rebuilt from the registry order on every click, so a toggle can only
   *  add or remove a source — it can never move one up or down the chain. */
  const toggle = (id: string) => {
    const next = rows
      .filter((r) => (r.id === id ? !ticked(id) : ticked(r.id)))
      .map((r) => r.id);
    // The last ticked row stays ticked: an empty list is the shipped default
    // on the server, so unticking it would tick everything back on.
    if (!next.length) return;
    void save(next);
  };

  const allTicked = rows.length > 0 && tickedCount === rows.length;

  return (
    <div className="relative">
      <button
        className={buttonClass}
        onClick={() => setOpen(!open)}
        title="Genre sources — which providers the import asks, and in what order"
        aria-label="Genre sources"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <SlidersHorizontal className="h-4 w-4" />
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        align={align}
        panelClass="w-[22rem] max-w-[calc(100vw-1rem)] max-h-[70vh] overflow-y-auto p-1.5"
      >
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2.5 pt-1 pb-0.5">
          Genre sources — asked top to bottom
        </div>
        {rows.map((r) => {
          const on = ticked(r.id);
          const last = on && tickedCount === 1;
          return (
            <label
              key={r.id}
              className={`flex items-start gap-2 px-2.5 py-1.5 rounded-lg ${
                saving ? "opacity-60" : "hover:bg-white/5 cursor-pointer"
              }`}
              title={last
                ? "At least one source must stay ticked — an empty list means every source"
                : undefined}
            >
              <input
                type="checkbox"
                className="mt-0.5 tap"
                checked={on}
                disabled={saving || last}
                onChange={() => toggle(r.id)}
              />
              <span className="min-w-0">
                <span className="flex items-center gap-1.5 text-xs text-zinc-200">
                  {r.rank != null && (
                    <span className="tabular-nums text-[10px] text-zinc-600">{r.rank}</span>
                  )}
                  {r.label}
                  {/* A source that needs a credential says so HERE and stays
                      tickable: the chain skips an unconfigured one and says
                      why, and unticking it for the user would quietly change
                      the list they saved. */}
                  {r.needs.length > 0 && !r.configured && (
                    <span
                      className="chip bg-amber-950/30 border border-amber-900/40 text-amber-300/90"
                      title={`The ticked source is skipped until you set: ${needsText(
                        r.needs,
                        ", "
                      )}`}
                    >
                      needs {needsText(r.needs, " + ")}
                    </span>
                  )}
                </span>
                <span className="block text-[11px] text-zinc-500 leading-snug">
                  {r.provides}
                </span>
              </span>
            </label>
          );
        })}
        <button
          className="btn-ghost w-full justify-start text-xs tap mt-1"
          disabled={saving || allTicked}
          onClick={() => void save(all)}
          title="The shipped default: every genre source the app knows, in this order"
        >
          All sources (default)
        </button>
      </Popover>
    </div>
  );
}
