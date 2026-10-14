import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown, ChevronRight, Disc3, FileVideo, FolderOpen, FolderInput, Loader2, Music2, Trash2,
} from "lucide-react";
import { api } from "../api";
import { fmtBytes } from "../lib/fmt";
import { toast } from "../store";
import type { DownloadEntry, DownloadEntryFile, DownloadsEntryDetail } from "../types";

/** One group the picker hands the import press: the files picked inside ONE
 *  top-level downloads entry — or, for the loose files that live at the
 *  downloads root, inside the root itself (they import as one album).
 *
 *  `path` is the BOX the server imports from (the entry's folder, or the
 *  downloads root), `files` the absolute paths inside it that were picked, and
 *  `tags` the non-empty tags the group's own files state — what the identify
 *  dialog opens its release row on. */
export interface DownloadPick {
  /** The top-level entry's name, or the downloads-root path for loose files. */
  entry: string;
  /** Absolute box passed to `importBulk` (`folder` for a root group). */
  path: string;
  /** The album folder name to create — the entry's own name for a folder, a
   *  name off the files' tags for a root group that has no folder to borrow. */
  name: string;
  /** Absolute paths of the picked files (all inside `path`). */
  files: string[];
  /** Every pickable (audio) file the group holds — what `files` is a subset
   *  of, for the dialog's "12 of 14 files". */
  total: number;
  bytes: number;
  tags: Record<string, string | null>;
}

/** Fold one file's non-empty tags into the group's map — the first file that
 *  states a fact wins (a group is one album, so they should agree; when they
 *  do not, the earlier file is as good as any). */
function mergeTags(dst: Record<string, string | null>, src: Record<string, string | null> | undefined) {
  if (!src) return;
  for (const [k, v] of Object.entries(src)) {
    if (v && !dst[k]) dst[k] = v;
  }
}

/** The tags a file-level identify row cares about, the ones worth a line. */
function tagFacts(f: DownloadEntryFile): string[] {
  const g = (k: string) => (f.tags?.[k] ?? "").trim();
  const bits: string[] = [];
  if (g("ALBUM")) bits.push(g("ALBUM"));
  const artist = g("ALBUMARTIST") || g("ARTIST");
  if (artist) bits.push(artist);
  if (g("DATE")) bits.push(g("DATE").slice(0, 4));
  const mb = g("MUSICBRAINZ_ALBUMID") || g("MUSICBRAINZ_RELEASEGROUPID");
  if (mb) bits.push(`MBID ${mb.slice(0, 8)}…`);
  return bits;
}

/** The downloads tab's picker: each top-level entry expands to the file list
 *  `api.downloadsEntry` reads, one checkbox per AUDIO file, and what is ticked
 *  is what the import takes.
 *
 *  The owner's ask: a manual import must land on the tracks meant, as one
 *  album, under the release meant. slskd drops an entry as a folder (an album)
 *  but a download often holds extra takes, a video, a scan — so picking is at
 *  the FILE, and the picked files of one entry import as that entry's album
 *  (the files picked at the root, which have no folder of their own, import as
 *  one album named off their tags).
 *
 *  Video / non-audio rows are SHOWN (they are part of what was downloaded, and
 *  their absence from the pick is a fact worth seeing) but never selectable:
 *  the import takes audio only, so a box that could be ticked and then silently
 *  dropped would lie. */
export default function DownloadPicker({
  folder, entries, busy, resetSignal, onPicksChange, onImport, onDelete,
}: {
  /** The downloads root — the parent of every entry, and the box a root group
   *  imports from. */
  folder: string;
  entries: DownloadEntry[];
  /** An import or delete is in flight: freeze every control. */
  busy: boolean;
  /** Bumped by the parent after an import/delete; drops the selection AND the
   *  cached file lists, because the listing they were read from just changed. */
  resetSignal: number;
  onPicksChange: (picks: DownloadPick[]) => void;
  onImport: () => void;
  /** Delete stays at ENTRY granularity: the whole download goes, picked or
   *  not — the hint under the toolbar says so. */
  onDelete: (names: string[]) => void;
}) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [details, setDetails] = useState<Record<string, DownloadsEntryDetail>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const [selectingAll, setSelectingAll] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // One in-flight read per entry: expanding is a press, and two presses (or a
  // select-all overlapping an expand) must not hit the server twice.
  const promiseRef = useRef<Map<string, Promise<DownloadsEntryDetail>>>(new Map());
  // The parent's setter is stable, but holding it in a ref keeps the emit
  // effect honest even if a caller ever passes an inline closure.
  const onPicksRef = useRef(onPicksChange);
  onPicksRef.current = onPicksChange;

  useEffect(() => {
    setPicked(new Set());
    setOpen(new Set());
    setDetails({});
    promiseRef.current = new Map();
    setConfirmDelete(false);
  }, [resetSignal]);

  /** Read one entry's file list, caching the promise so concurrent callers
   *  share a single request (and its cached result afterwards). */
  const loadDetail = (name: string): Promise<DownloadsEntryDetail> => {
    const cached = details[name];
    if (cached) return Promise.resolve(cached);
    const existing = promiseRef.current.get(name);
    if (existing) return existing;
    setLoading((s) => new Set(s).add(name));
    const p = api.downloadsEntry(name)
      .then((d) => {
        setDetails((cur) => ({ ...cur, [name]: d }));
        return d;
      })
      .catch((e) => {
        toast.error(String(e));
        throw e;
      })
      .finally(() => {
        promiseRef.current.delete(name);
        setLoading((s) => {
          const n = new Set(s);
          n.delete(name);
          return n;
        });
      });
    promiseRef.current.set(name, p);
    return p;
  };

  const toggleOpen = (e: DownloadEntry) => {
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(e.name)) n.delete(e.name);
      else n.add(e.name);
      return n;
    });
    if (!details[e.name]) void loadDetail(e.name).catch(() => {});
  };

  const toggleFile = (f: DownloadEntryFile) => {
    setConfirmDelete(false);
    setPicked((s) => {
      const n = new Set(s);
      if (n.has(f.path)) n.delete(f.path);
      else n.add(f.path);
      return n;
    });
  };

  const toggleEntryAll = (e: DownloadEntry, on: boolean) => {
    const paths = (details[e.name]?.files ?? []).filter((f) => f.audio).map((f) => f.path);
    setConfirmDelete(false);
    setPicked((s) => {
      const n = new Set(s);
      for (const p of paths) {
        if (on) n.add(p);
        else n.delete(p);
      }
      return n;
    });
  };

  /** Select every audio file of every entry — which means reading the entries
   *  not yet expanded first, so "all" really is all. */
  const selectAll = async () => {
    setSelectingAll(true);
    try {
      const rows = await Promise.all(entries.map((e) => loadDetail(e.name).catch(() => null)));
      const n = new Set<string>();
      for (const d of rows) for (const f of d?.files ?? []) if (f.audio) n.add(f.path);
      setPicked(n);
    } finally {
      setSelectingAll(false);
    }
  };

  const selectNone = () => {
    setConfirmDelete(false);
    setPicked(new Set());
  };

  /** The picked files, grouped by their top-level entry — the shape both the
   *  summary and the import press speak. */
  const picks = useMemo<DownloadPick[]>(() => {
    const groups = new Map<string, DownloadPick>();
    for (const e of entries) {
      const key = e.dir ? e.name : folder;
      const d = details[e.name];
      const pickedFiles = (d?.files ?? []).filter((f) => f.audio && picked.has(f.path));
      if (!pickedFiles.length) continue;
      let g = groups.get(key);
      if (!g) {
        g = {
          entry: key,
          path: e.dir ? `${folder}/${e.name}` : folder,
          name: e.dir ? e.name : "",
          files: [],
          total: 0,
          bytes: 0,
          tags: {},
        };
        groups.set(key, g);
      }
      for (const f of pickedFiles) {
        g.files.push(f.path);
        g.bytes += f.size || 0;
        mergeTags(g.tags, f.tags);
      }
      if (e.dir) g.total = d?.count ?? e.files;
    }
    // The root group's own "of N" is every loose file the listing shows (not
    // just the picked ones), and — with no folder to borrow one from — its
    // album name comes off the files' tags.
    const loose = entries.filter((e) => !e.dir).length;
    for (const g of groups.values()) {
      if (g.path === folder) {
        g.total = loose;
        // No folder to borrow a name from: the files' own ALBUM tag, else the
        // first file's name without its extension.
        g.name = (g.tags.ALBUM || "").trim()
          || (g.files[0] ?? "").split(/[\\/]/).pop()?.replace(/\.[^.]+$/, "")
          || "downloads";
      }
    }
    return [...groups.values()].sort((a, b) => a.entry.localeCompare(b.entry));
  }, [entries, details, picked, folder]);

  useEffect(() => {
    onPicksRef.current(picks);
  }, [picks]);

  const pickedCount = picks.reduce((n, p) => n + p.files.length, 0);
  const pickedBytes = picks.reduce((n, p) => n + p.bytes, 0);
  const names = picks.map((p) => p.entry);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <button
          className="btn-ghost !py-1 text-xs tap"
          onClick={() => void selectAll()}
          disabled={busy || selectingAll || entries.length === 0}
          title="Read every entry and tick every audio file it holds"
        >
          {selectingAll ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Select all
        </button>
        {pickedCount > 0 && (
          <button className="btn-ghost !py-1 text-xs tap" onClick={selectNone} disabled={busy}>
            Select none
          </button>
        )}
        {pickedCount > 0 && (
          <span className="text-xs text-zinc-400">
            {pickedCount} file{pickedCount === 1 ? "" : "s"} picked across {picks.length} download
            {picks.length === 1 ? "" : "s"} · {fmtBytes(pickedBytes)}
          </span>
        )}
        <div className="flex flex-wrap items-center gap-2 ml-auto justify-end">
          <button
            className="btn-primary !py-1 text-xs tap"
            onClick={onImport}
            disabled={busy || pickedCount === 0}
            title="Ask which release each picked group is (its row opens on the release its files name), then move just the picked files into the library and run the import chain on them"
          >
            <FolderInput className="h-3.5 w-3.5" />{" "}
            {picks.length > 1 ? `Import ${picks.length} downloads` : "Import to library"}
          </button>
          {confirmDelete ? (
            <>
              <span className="text-xs text-amber-300">
                Delete {names.length} whole download{names.length === 1 ? "" : "s"}?
              </span>
              <button className="btn-danger !py-1 text-xs tap" onClick={() => onDelete(names)} disabled={busy}>
                <Trash2 className="h-3.5 w-3.5" /> Yes, delete
              </button>
              <button className="btn-ghost !py-1 text-xs tap" onClick={() => setConfirmDelete(false)} disabled={busy}>
                Cancel
              </button>
            </>
          ) : (
            <button
              className="btn-danger !py-1 text-xs tap"
              onClick={() => setConfirmDelete(true)}
              disabled={busy || pickedCount === 0}
              title="Delete the selected downloads from disk — the WHOLE entry, picked files or not, and it cannot be undone"
            >
              <Trash2 className="h-3.5 w-3.5" /> Delete
            </button>
          )}
        </div>
      </div>

      <div className="space-y-1.5">
        {entries.map((e) => {
          const isOpen = open.has(e.name);
          const d = details[e.name];
          const isLoading = loading.has(e.name) || (isOpen && !d);
          const files = d?.files ?? [];
          const pickable = files.filter((f) => f.audio);
          const pickedHere = pickable.filter((f) => picked.has(f.path)).length;
          const allHere = pickable.length > 0 && pickedHere === pickable.length;
          return (
            <div
              key={e.name}
              className={`bg-card rounded-lg border ${pickedHere > 0 ? "border-accent/60" : "border-border"} ${
                e.partial ? "opacity-60" : ""
              }`}
            >
              <button
                type="button"
                onClick={() => toggleOpen(e)}
                className="w-full flex items-center gap-3 px-3 py-2 text-left tap"
                title={e.partial ? "Still downloading or a scratch file — leave it alone" : e.name}
              >
                {isOpen ? (
                  <ChevronDown className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                )}
                {e.dir ? (
                  <Disc3 className="h-4 w-4 text-zinc-500 shrink-0" />
                ) : (
                  <FolderOpen className="h-4 w-4 text-zinc-500 shrink-0" />
                )}
                <span className="flex-1 min-w-0">
                  <span className="block truncate text-sm text-zinc-200">{e.name}</span>
                  <span className="block text-[11px] text-zinc-500">
                    {fmtBytes(e.bytes)}
                    {e.dir && ` · ${e.files} file(s)`}
                    {e.audio > 0 && ` · ${e.audio} audio`}
                    {e.images > 0 && ` · ${e.images} image(s)`}
                    {!e.album && e.audio === 0 && " · no audio"}
                  </span>
                </span>
                {pickedHere > 0 && (
                  <span className="chip bg-accent/15 text-accent-soft border border-accent/30 shrink-0">
                    {pickedHere} picked
                  </span>
                )}
                {e.partial && (
                  <span className="chip bg-amber-950/40 text-amber-300 border border-amber-900 shrink-0">
                    in-flight
                  </span>
                )}
                {!e.partial && e.album && (
                  <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800 shrink-0">
                    importable
                  </span>
                )}
                {!e.partial && !e.album && (
                  <span className="chip bg-raise border border-border text-zinc-500 shrink-0">no audio</span>
                )}
              </button>

              {isOpen && (
                <div className="border-t border-border/60 px-3 py-2 space-y-0.5">
                  {isLoading && (
                    <div className="flex items-center gap-2 text-xs text-zinc-500 py-1">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> Reading files…
                    </div>
                  )}
                  {d?.truncated && (
                    <div className="text-[10px] text-amber-300/80">
                      Showing the first {d.files.length} of {d.count} files.
                    </div>
                  )}
                  {d && pickable.length > 0 && (
                    <label className="flex items-center gap-2 text-[11px] text-zinc-400 pb-1 cursor-pointer">
                      <input type="checkbox" checked={allHere} onChange={() => toggleEntryAll(e, !allHere)} />
                      {allHere ? "None" : `All ${pickable.length} audio file${pickable.length === 1 ? "" : "s"}`}
                    </label>
                  )}
                  {d && files.length === 0 && <div className="text-[11px] text-zinc-600 py-1">No files.</div>}
                  {files.map((f) => {
                    const selectable = f.audio;
                    const on = picked.has(f.path);
                    const facts = tagFacts(f);
                    return (
                      <label
                        key={f.path}
                        className={`flex items-start gap-2 rounded-md px-1.5 py-1 ${
                          selectable ? "cursor-pointer hover:bg-raise/40" : "opacity-50"
                        } ${on ? "bg-accent/10" : ""}`}
                      >
                        {selectable ? (
                          <input
                            type="checkbox"
                            className="mt-0.5 shrink-0"
                            checked={on}
                            onChange={() => toggleFile(f)}
                          />
                        ) : (
                          <span
                            className="mt-0.5 w-3.5 shrink-0 inline-flex justify-center text-zinc-600"
                            title="Not audio — shown for context, never imported"
                          >
                            {f.is_video ? <FileVideo className="h-3.5 w-3.5" /> : <Music2 className="h-3.5 w-3.5" />}
                          </span>
                        )}
                        <span className="flex-1 min-w-0">
                          <span className="block truncate text-xs text-zinc-300" title={f.path}>
                            {f.name}
                          </span>
                          <span className="block text-[10px] text-zinc-500">
                            {fmtBytes(f.size)}
                            {facts.length > 0 && ` · ${facts.join(" · ")}`}
                          </span>
                        </span>
                        {!selectable && (
                          <span className="chip bg-raise border border-border text-zinc-500 shrink-0">
                            {f.is_video ? "video" : "not audio"}
                          </span>
                        )}
                      </label>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="text-[10px] text-zinc-600">
        Expand a download to tick the tracks you want; the ticked files of one download import as its album, and
        loose files at the root import together as one. Delete stays whole-download: it removes the entire entry
        from disk (ticked or not) and nothing is copied to the trash first.
      </div>
    </div>
  );
}