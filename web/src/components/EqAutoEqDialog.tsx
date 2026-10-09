import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Headphones, Loader2, RefreshCw, Search } from "lucide-react";
import { api, type EqAutoEqRow, type ExportEqProfile } from "../api";
import Modal from "./Modal";
import { toast } from "../store";

/** Find a headphone in AutoEq and import its correction as a profile.
 *
 *  AutoEq is a catalogue of published headphone measurements
 *  (github.com/jaakkopasanen/AutoEq), and every model it has equalized is one
 *  `ParametricEQ.txt` in this app's own profile format — so importing one is
 *  the same store, the same parser and the same player EQ as pasting a file by
 *  hand, with the search doing the finding. The server keeps the project's own
 *  index cached (a month) and fetches the one file the user picks, so this
 *  dialog never touches GitHub itself. */
export default function EqAutoEqDialog({
  onClose,
  onImported,
}: {
  onClose: () => void;
  /** The stored profile, so the caller can select it immediately. */
  onImported: (row: ExportEqProfile) => void;
}) {
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const qc = useQueryClient();

  // Typing must not fire a request per keystroke: the query is committed once
  // it has been still for a moment, which is also when the user has finished a
  // word ("sennheiser hd 6…" answers nothing useful).
  useEffect(() => {
    const id = window.setTimeout(() => setQuery(text.trim()), 300);
    return () => window.clearTimeout(id);
  }, [text]);

  const search = useQuery({
    queryKey: ["eqAutoEq", query],
    queryFn: () => api.eqAutoEqSearch(query),
    enabled: query.length >= 2,
    staleTime: 5 * 60_000,
    retry: false,
  });

  /** Re-fetch the catalogue itself. Imperative rather than part of the query
   *  key: the cache is what makes typing cheap, so a refresh must not become a
   *  property of every later search. */
  const refreshIndex = async () => {
    try {
      const data = await api.eqAutoEqSearch(query, true);
      qc.setQueryData(["eqAutoEq", query], data);
      toast(data.error
        ? data.error
        : `AutoEq index updated — ${data.models.toLocaleString()} measurements`);
    } catch (e) {
      toast.error(String(e));
    }
  };

  const importOne = async (row: EqAutoEqRow) => {
    setBusy(row.id);
    try {
      const profile = await api.eqAutoEqImport(row.id);
      toast.success(`Imported ${profile.label}`);
      onImported(profile);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy("");
    }
  };

  const rows = search.data?.rows ?? [];
  const models = search.data?.models ?? 0;

  return (
    <Modal
      onClose={onClose}
      icon={Headphones}
      title="AutoEq headphones"
      subtitle="Search a measured headphone, import its correction as a profile"
      width="max-w-2xl"
      bodyClass="px-5 py-4 space-y-3"
    >
      <div className="flex items-center gap-2">
        <div className="flex-1 flex items-center gap-2 rounded-md border border-border bg-panel px-2.5 py-1.5">
          <Search className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
          <input
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Headphone model — “Sennheiser HD 600”, “Moondrop Aria”…"
            className="flex-1 bg-transparent text-sm outline-none"
            aria-label="Search AutoEq headphones"
          />
          {search.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-500" />}
        </div>
        <button
          className="btn-ghost !py-1.5 text-xs shrink-0"
          onClick={() => void refreshIndex()}
          disabled={search.isFetching}
          title="Re-fetch AutoEq's index — use it when a headphone is missing"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh list
        </button>
      </div>

      {/* The catalogue is the project's own index, cached for a month; a failed
          refresh still answers from the cache, so the reason is shown WITHOUT
          hiding the results it is still serving. */}
      {search.data?.error ? (
        <p className="text-[11px] text-amber-300/90">{search.data.error}</p>
      ) : null}
      {search.data && !search.data.error ? (
        <p className="text-[11px] text-zinc-500">
          {models.toLocaleString()} measurements indexed
          {search.data.fetched_at
            ? ` · updated ${new Date(search.data.fetched_at * 1000).toLocaleDateString()}`
            : ""}
        </p>
      ) : null}

      <div className="max-h-[50vh] overflow-y-auto overscroll-contain rounded-md border border-border divide-y divide-border/60">
        {query.length < 2 ? (
          <p className="px-3 py-6 text-center text-xs text-zinc-500">
            Type at least two characters of the model name.
          </p>
        ) : search.isError ? (
          <p className="px-3 py-6 text-center text-xs text-red-300">{String(search.error)}</p>
        ) : !rows.length && !search.isFetching ? (
          <p className="px-3 py-6 text-center text-xs text-zinc-500">
            Nothing matched. AutoEq's list is what has been MEASURED — try the model without its
            suffix, or paste the correction from the headphone's own page instead.
          </p>
        ) : (
          rows.map((row) => (
            <div key={row.id} className="flex items-center gap-3 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="text-sm text-zinc-200 truncate">{row.model}</div>
                <div className="text-[11px] text-zinc-500 truncate">
                  {[row.source, row.rig].filter(Boolean).join(" · ")}
                </div>
              </div>
              <button
                className="btn-ghost !py-1 text-xs shrink-0"
                onClick={() => importOne(row)}
                disabled={!!busy}
                title="Fetch this measurement's correction and store it as a profile"
              >
                {busy === row.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Import
              </button>
            </div>
          ))
        )}
      </div>

      <p className="text-[11px] text-zinc-500">
        Imported curves are stored as ordinary profiles: they show up in the profile list, apply to
        playback like any other, and can be edited band by band. The correction is the measurement
        source's, applied to AutoEq's own target curve.
      </p>
    </Modal>
  );
}
