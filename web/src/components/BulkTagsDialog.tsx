import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import { invalidateLibrary } from "../lib/invalidate";
import Modal from "./Modal";

// Common tags worth offering as one-click removals — everything else can
// be typed. Deliberately EXCLUDES identity tags (TITLE/ARTIST/ALBUM/
// TRACKNUMBER/…): bulk-deleting those wrecks a library; structural tags
// are managed by the scripts instead.
const COMMON_REMOVABLE = [
  "GENRE", "MOOD", "COMMENT", "COMMENTARY", "BPM", "INITIALKEY",
  "MEDIA", "SOURCE", "CATALOGNUMBER", "BARCODE", "LABEL", "LANGUAGE",
  "RYMALBUM", "RYM_LINK", "DISCOGS_LINK", "WORK", "GROUPING",
  "RELEASECOUNTRY", "MUSICBRAINZ_ALBUMID",
];

interface SetRow { name: string; value: string }

/** Bulk remove / set tags across a chosen set of tracks. Two-step apply
 * (arm, then confirm) so a stray click never rewrites a batch. */
export default function BulkTagsDialog({
  paths,
  onClose,
}: {
  paths: string[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [removals, setRemovals] = useState<string[]>([]);
  const [customRemove, setCustomRemove] = useState("");
  const [setRows, setSetRows] = useState<SetRow[]>([{ name: "", value: "" }]);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  const toggleRemove = (tag: string) =>
    setRemovals((r) => (r.includes(tag) ? r.filter((t) => t !== tag) : [...r, tag]));

  const apply = async () => {
    if (busy) return;
    const remove = [...removals, ...customRemove.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean)];
    const set: Record<string, string> = {};
    for (const r of setRows) {
      if (r.name.trim()) set[r.name.trim().toUpperCase()] = r.value;
    }
    if (!remove.length && !Object.keys(set).length) {
      toast("Pick tags to remove or add a tag to set first");
      return;
    }
    setBusy(true);
    try {
      const res = await api.tagsBulk({ paths, remove, set });
      toast(`Tags updated on ${paths.length} track(s) — ${res.added} set, ${res.removed} removed${res.failed ? `, ${res.failed} failed` : ""}`);
      invalidateLibrary(qc);
      onClose();
    } catch (e) {
      toast(`Bulk tag failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      onClose={onClose}
      title="Bulk tag editor"
      subtitle={`${paths.length} track${paths.length === 1 ? "" : "s"} selected`}
      bodyClass="px-5 py-4 space-y-5"
      footer={
        <div className="flex items-center gap-2">
          <div className="text-[10px] text-zinc-600 flex-1 leading-snug">
            Applied directly to the selected files. Structural tags (TITLE, ARTIST, TRACKNUMBER…) are
            best left to the scripts.
          </div>
          {!armed ? (
            <button className="btn-ghost !py-1.5 text-xs" onClick={() => setArmed(true)} title="Arm the bulk apply">
              Apply…
            </button>
          ) : (
            <button className="btn-primary !py-1.5 text-xs bg-red-600 hover:bg-red-500" onClick={apply} disabled={busy}>
              {busy ? "Applying…" : `Confirm on ${paths.length} track(s)`}
            </button>
          )}
          <button className="btn-ghost !py-1.5 text-xs" onClick={onClose}>
            Cancel
          </button>
        </div>
      }
    >
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 pb-2">Remove tags</div>
        <div className="flex flex-wrap gap-1.5">
          {COMMON_REMOVABLE.map((tag) => (
            <button
              key={tag}
              className={`chip text-[10px] border ${removals.includes(tag) ? "bg-red-950/60 border-red-700/60 text-red-200" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
              onClick={() => toggleRemove(tag)}
            >
              {tag}
            </button>
          ))}
        </div>
        <input
          className="input !py-1.5 text-xs mt-2.5 w-full"
          placeholder="More tags to remove, comma separated (e.g. CUSTOM_TAG, TXXX_NOTE)"
          value={customRemove}
          onChange={(e) => setCustomRemove(e.target.value)}
        />
      </div>

      <div>
        <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 pb-2">Set / add tags</div>
        <div className="space-y-1.5">
          {setRows.map((row, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <input
                className="input !py-1.5 text-xs w-40 shrink-0 font-mono uppercase"
                placeholder="TAGNAME"
                value={row.name}
                onChange={(e) => setSetRows((rows) => rows.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))}
              />
              <input
                className="input !py-1.5 text-xs flex-1 min-w-0"
                placeholder="Value (empty = remove this tag)"
                value={row.value}
                onChange={(e) => setSetRows((rows) => rows.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))}
              />
              <button
                className="p-1.5 rounded text-zinc-500 hover:text-red-400 hover:bg-white/5 shrink-0"
                onClick={() => setSetRows((rows) => rows.filter((_, j) => j !== i))}
                disabled={setRows.length === 1}
                title="Remove this row"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          <button className="btn-ghost !py-1 text-xs flex items-center gap-1.5" onClick={() => setSetRows((rows) => [...rows, { name: "", value: "" }])}>
            <Plus className="h-3.5 w-3.5" /> Add another tag
          </button>
        </div>
      </div>
    </Modal>
  );
}
