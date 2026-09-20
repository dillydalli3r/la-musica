import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { invoke } from "@tauri-apps/api/core";
import { ArrowUp, Disc3, FolderOpen, Loader2, Lock } from "lucide-react";
import Modal from "./Modal";
import { api, IN_TAURI } from "../api";
import { toast } from "../store";

type DirEntry = { name: string; path: string; library: boolean; writable: boolean };
type Listing = {
  path: string;
  parent: string | null;
  roots: string[];
  dirs: DirEntry[];
  /** Set when MLO_MUSIC_FOLDER pins the folder (Docker/compose): browsing
   *  still works, but a choice here cannot stick — see save(). */
  pinned: string | null;
};

/** Pick the music folder — the library the whole app reads and writes.
 *
 *  The folder is a path the BACKEND opens, and no browser can hand it one from
 *  the client's own machine, so this browses the SERVER's filesystem
 *  (GET /api/fs/dirs — directory names only, nothing is opened). The desktop
 *  shell additionally offers the OS dialog, because that shell runs the backend
 *  on this very machine; the native path then means the same thing to both
 *  sides.
 *
 *  Saving goes through POST /api/config, which validates that the folder
 *  exists, carries `<music>/.mlo` (config, playlists, auth, downloads, trash)
 *  to the new folder, and tells a running slskd to re-read its shares — so a
 *  switch here is complete, not just a stored string. */
export default function FolderPicker({
  startPath,
  onClose,
  onPicked,
}: {
  /** Where to open; defaults to the folder currently in use. */
  startPath?: string;
  onClose: () => void;
  onPicked: (path: string) => void;
}) {
  const qc = useQueryClient();
  const [target, setTarget] = useState(startPath ?? "");
  const [listing, setListing] = useState<Listing | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .fsDirs(target)
      .then((r) => {
        if (!live) return;
        setListing(r);
        setTarget(r.path);
        setError("");
      })
      .catch((e) => {
        if (!live) return;
        setListing(null);
        setError(String(e));
      });
    return () => {
      live = false;
    };
  }, [target]);

  const save = async (choice: string) => {
    if (!choice || listing?.pinned) return;
    setBusy(true);
    try {
      await api.saveConfig({ music_folder: choice });
      qc.invalidateQueries({ queryKey: ["config"] });
      toast.success(`Music folder set to ${choice}`);
      onPicked(choice);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  /** The OS dialog, desktop shell only (the mobile shells have no such API and
   *  this shell's backend is this machine, so the path it returns is valid). */
  const pickNative = async () => {
    setBusy(true);
    try {
      const picked = await invoke<string | null>("pick_folder");
      if (picked) await save(picked);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const here = listing?.path ?? target;

  return (
    <Modal
      onClose={onClose}
      icon={FolderOpen}
      title="Music folder"
      subtitle="The library this server reads and writes. Its own state lives in a hidden .mlo folder inside it."
      width="max-w-2xl"
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[11px] text-zinc-500 font-mono truncate max-w-[60%]">{here || "…"}</span>
          <div className="flex gap-2">
            <button className="btn-ghost !py-1.5 text-xs" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button
              className="btn-primary !py-1.5 text-xs"
              onClick={() => void save(here)}
              disabled={busy || !listing || !!listing.pinned}
              title={listing?.pinned ? `Pinned to ${listing.pinned} by MLO_MUSIC_FOLDER` : undefined}
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Use this folder"}
            </button>
          </div>
        </div>
      }
    >
      <div className="space-y-3">
        {listing?.pinned && (
          <div className="rounded-md border border-amber-900 bg-amber-950/30 px-3 py-2 text-[11px] text-amber-300 leading-relaxed">
            This server's music folder is pinned to <code className="font-mono">{listing.pinned}</code> by{" "}
            <code className="font-mono">MLO_MUSIC_FOLDER</code> — that is what the app USES, and a folder chosen here
            would be rewritten back to it. Change the variable (docker-compose.yml, or the environment the server starts
            in) and restart; remove it to pick a folder here.
          </div>
        )}
        {/* Where we are, and the ways out: up one level, or straight to a root
            (a drive on Windows, "/" and the home folder elsewhere). */}
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="btn-ghost !py-1 text-xs"
            disabled={!listing?.parent}
            onClick={() => listing?.parent && setTarget(listing.parent)}
          >
            <ArrowUp className="h-3.5 w-3.5" /> Up
          </button>
          {(listing?.roots ?? []).map((root) => (
            <button key={root} className="btn-ghost !py-1 text-xs font-mono" onClick={() => setTarget(root)}>
              {root}
            </button>
          ))}
          {IN_TAURI && (
            <button className="btn-ghost !py-1 text-xs ml-auto" onClick={() => void pickNative()} disabled={busy}>
              <FolderOpen className="h-3.5 w-3.5" /> Use the OS dialog…
            </button>
          )}
        </div>

        {error && (
          <div className="rounded-md border border-red-900 bg-red-950/40 px-3 py-2 text-[11px] text-red-300">
            {error}
          </div>
        )}

        <div className="rounded-md border border-border max-h-[45vh] overflow-y-auto divide-y divide-border/60">
          {!listing && !error && (
            <div className="px-3 py-6 text-center text-xs text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin inline" /> Reading {here || "the folder"}…
            </div>
          )}
          {listing && listing.dirs.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-zinc-500">
              No sub-folders here — “Use this folder” takes the one you are in.
            </div>
          )}
          {(listing?.dirs ?? []).map((d) => (
            <button
              key={d.path}
              className="w-full flex items-center gap-2 px-3 py-2 text-left text-xs text-zinc-300 hover:bg-panel/60 tap"
              onClick={() => setTarget(d.path)}
            >
              <FolderOpen className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
              <span className="truncate">{d.name}</span>
              {d.library && (
                <span
                  className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800"
                  title="This folder holds audio files — a likely library root"
                >
                  <Disc3 className="h-3 w-3" /> library?
                </span>
              )}
              {!d.writable && (
                <span className="chip bg-zinc-800 text-zinc-400 border border-zinc-700" title="Not writable by the server">
                  <Lock className="h-3 w-3" /> read-only
                </span>
              )}
            </button>
          ))}
        </div>

        <p className="text-[11px] text-zinc-600 leading-relaxed">
          Switching folders MOVES the app's own state (<code className="font-mono">&lt;music&gt;/.mlo</code>: config,
          playlists, sessions, downloads, trash) into the new one, and a running Soulseek daemon is restarted so it
          shares the new tree. Your music files themselves are not moved.
        </p>
      </div>
    </Modal>
  );
}
