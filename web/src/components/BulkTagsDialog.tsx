import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import { invalidateLibrary } from "../lib/invalidate";
import { useTagRegistry } from "../lib/tags";
import Modal from "./Modal";

interface SetRow { name: string; value: string }

/** Bulk remove / set tags across a chosen set of tracks. Two-step apply
 * (arm, then confirm) so a stray click never rewrites a batch.
 *
 *  The chips, their labels and their grouping all come from the tag registry
 *  (server/tags_registry.py), so this dialog shows the same names and families
 *  the track page and the Grading page do. The one rule kept here is which
 *  tags may be offered: everything EXCEPT the tags the grader's required-tags
 *  sweep fails an album for — bulk-deleting one of those wrecks a library, and
 *  the registry says which they are (graded_by). */
export default function BulkTagsDialog({
  paths,
  onClose,
}: {
  paths: string[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const reg = useTagRegistry();
  const [removals, setRemovals] = useState<string[]>([]);
  const [customRemove, setCustomRemove] = useState("");
  const [setRows, setSetRows] = useState<SetRow[]>([{ name: "", value: "" }]);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  const toggleRemove = (tag: string) =>
    setRemovals((r) => (r.includes(tag) ? r.filter((t) => t !== tag) : [...r, tag]));

  // Which tag a set-row names, resolved through the registry, decides the
  // value hint (a closed value set says which values it takes).
  const genreRow = setRows.some((r) => r.name.trim().toUpperCase() === "GENRE");

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
        <div className="space-y-2 max-h-56 overflow-auto pr-1">
          {(reg?.families ?? []).map((family) => {
            const tags = (reg?.tags ?? []).filter(
              (t) => t.family === family.id && !t.graded_by.includes("grade_check_missing_tags"),
            );
            if (!tags.length) return null;
            return (
              <div key={family.id} className="space-y-1">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-600">{family.label}</div>
                <div className="flex flex-wrap gap-1.5">
                  {tags.map((tag) => (
                    <button
                      key={tag.key}
                      className={`chip text-[10px] border ${removals.includes(tag.key) ? "bg-red-950/60 border-red-700/60 text-red-200" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
                      title={`${tag.key} — ${tag.meaning}`}
                      onClick={() => toggleRemove(tag.key)}
                    >
                      {tag.label}
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
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
          {setRows.map((row, i) => {
            // The registry's entry for the name typed so far, for the value hint.
            const info = reg?.tags.find((t) => t.key === row.name.trim().toUpperCase());
            return (
            <div key={i} className="flex items-center gap-1.5">
              <input
                className="input !py-1.5 text-xs w-40 shrink-0 font-mono uppercase"
                placeholder="TAGNAME"
                list="mlo-tag-keys"
                value={row.name}
                onChange={(e) => setSetRows((rows) => rows.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))}
              />
              <input
                className="input !py-1.5 text-xs flex-1 min-w-0"
                placeholder={info?.enum ? `One of ${info.enum.join(" / ")} — empty = remove` : "Value (empty = remove this tag)"}
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
            );
          })}
          <button className="btn-ghost !py-1 text-xs flex items-center gap-1.5" onClick={() => setSetRows((rows) => [...rows, { name: "", value: "" }])}>
            <Plus className="h-3.5 w-3.5" /> Add another tag
          </button>
        </div>
        {/* The name input completes from the registry, so a canonical key (and
            its label) is one keystroke away instead of typed from memory. */}
        <datalist id="mlo-tag-keys">
          {(reg?.tags ?? []).map((tag) => (
            <option key={tag.key} value={tag.key}>{tag.label}</option>
          ))}
        </datalist>
        {genreRow && (
          <div className="mt-2 text-[10px] text-zinc-500 leading-snug rounded border border-border/60 bg-panel/40 px-2 py-1.5">
            <span className="font-mono text-zinc-400">GENRE</span> is a LIST, not one string: the specific
            genres first and the family last — <span className="text-zinc-400">shoegaze / dream pop / rock</span>.
            Write MusicBrainz spellings (lowercase, as MusicBrainz publishes them); the server splits and
            canonicalises what it reads, and a name MusicBrainz does not know fails the vocabulary check
            (GENRE_VOCAB) until Auto tagging (8) or Format all (10) rewrites it.
          </div>
        )}
      </div>
    </Modal>
  );
}
