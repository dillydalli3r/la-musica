import { useEffect, useState } from "react";
import { ArrowUp, Folder, HardDrive, Loader2, X } from "lucide-react";
import { api } from "../api";

interface Props {
  initial?: string;
  onPick: (path: string) => void;
  onClose: () => void;
}

/** Server-backed folder browser. Browsers never reveal an absolute path, so
 * this walks the disk through /api/fs/list instead of the native picker. */
export default function FolderPicker({ initial, onPick, onClose }: Props) {
  const [path, setPath] = useState(initial || "");
  const [typed, setTyped] = useState(initial || "");
  const [dirs, setDirs] = useState<string[]>([]);
  const [parent, setParent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const open = async (p: string) => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.fsList(p);
      setPath(r.path);
      setTyped(r.path);
      setDirs(r.dirs);
      setParent(r.parent);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    open(initial || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const base = (p: string) => p.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl border border-border bg-card shadow-2xl flex flex-col max-h-[80vh]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="text-sm font-semibold">Choose a music folder</div>
          <button className="btn-ghost !p-1" onClick={onClose} title="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        <form
          className="flex gap-2 px-4 py-3 border-b border-border"
          onSubmit={(e) => {
            e.preventDefault();
            open(typed.trim());
          }}
        >
          <input
            className="input !py-1 text-xs font-mono"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder="Type a path, e.g. F:\Music"
            autoFocus
          />
          <button className="btn-ghost !py-1 text-xs shrink-0" type="submit">
            Go
          </button>
        </form>

        <div className="flex-1 overflow-auto p-2">
          {loading && (
            <div className="flex items-center gap-2 px-3 py-6 text-xs text-zinc-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
            </div>
          )}
          {error && <div className="px-3 py-3 text-xs text-red-400">{error}</div>}
          {!loading && !error && (
            <>
              {parent !== null && (
                <button
                  className="w-full flex items-center gap-2 px-3 py-1.5 rounded-md hover:bg-white/10 text-left text-xs text-zinc-300"
                  onClick={() => open(parent)}
                >
                  <ArrowUp className="h-3.5 w-3.5 shrink-0 text-zinc-500" /> ..
                </button>
              )}
              {dirs.length === 0 && (
                <div className="px-3 py-2 text-xs text-zinc-500">No sub-folders here.</div>
              )}
              {dirs.map((d) => (
                <button
                  key={d}
                  className="w-full flex items-center gap-2 px-3 py-1.5 rounded-md hover:bg-white/10 text-left text-xs text-zinc-200"
                  onClick={() => open(d)}
                  title={d}
                >
                  {/^[A-Za-z]:[\\/]?$/.test(d) ? (
                    <HardDrive className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
                  ) : (
                    <Folder className="h-3.5 w-3.5 shrink-0 text-accent-soft" />
                  )}
                  <span className="truncate">{base(d)}</span>
                </button>
              ))}
            </>
          )}
        </div>

        <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-border">
          <div className="text-[11px] font-mono text-zinc-500 truncate" title={path}>
            {path || "This computer"}
          </div>
          <div className="flex gap-2 shrink-0">
            <button className="btn-ghost !py-1 text-xs" onClick={onClose}>
              Cancel
            </button>
            <button
              className="btn-primary !py-1 text-xs"
              disabled={!path || loading}
              onClick={() => {
                onPick(path);
                onClose();
              }}
            >
              Use this folder
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
