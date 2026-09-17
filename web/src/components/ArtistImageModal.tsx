import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, ImageUp, Loader2, RefreshCw } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import Modal from "./Modal";

/** Artist image picker: every candidate the configured sources offer, plus a
 *  manual upload. Clicking a candidate saves it immediately (the automatic
 *  fetch only ever takes the first one — this is how a bad automatic pick is
 *  swapped for another provider's). The upload path is also what the page
 *  offers when no source has an image at all. */
export default function ArtistImageModal({
  artist,
  onClose,
  onSaved,
}: {
  /** Artist folder path (or plain name) — what the artwork API takes. */
  artist: string;
  onClose: () => void;
  /** Called after the image changed; the page refreshes its artist query. */
  onSaved: () => void;
}) {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["artistImageCandidates", artist],
    queryFn: () => api.artistImageCandidates(artist),
    retry: false,
  });
  const [busy, setBusy] = useState<string | null>(null);
  const uploadInput = useRef<HTMLInputElement>(null);
  const rows = data?.rows ?? [];

  const pick = async (url: string, source: string) => {
    setBusy(url);
    try {
      await api.artistImageSave(artist, url, source);
      toast("Artist image saved");
      onSaved();
      onClose();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const upload = async (file: File) => {
    setBusy("upload");
    try {
      await api.artistImageUpload(artist, file);
      toast("Artist image uploaded");
      onSaved();
      onClose();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
      if (uploadInput.current) uploadInput.current.value = "";
    }
  };

  return (
    <Modal
      onClose={onClose}
      title="Artist image"
      subtitle="Pick a candidate — it is cropped square and stored in the artist folder as artist.jpg."
      width="max-w-[640px]"
      bodyClass="px-5 py-5"
      headerExtra={
        <button
          className="p-1.5 rounded-lg hover:bg-raise text-zinc-500 hover:text-white shrink-0"
          onClick={() => refetch()}
          disabled={isFetching}
          title="Search the sources again"
        >
          <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
        </button>
      }
    >
      {isLoading ? (
        <div className="flex items-center justify-center gap-2 py-10 text-xs text-zinc-500">
          <Loader2 className="h-4 w-4 animate-spin" /> Looking for artist images…
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border bg-raise/40 p-6 text-center">
          <div className="text-sm text-zinc-300">No candidate images found</div>
          <div className="text-xs text-zinc-500 mt-1 max-w-sm mx-auto">
            None of the configured sources returned a photo for this artist. Upload one from your
            machine — it is saved as artist.jpg in the artist folder and counts for the artist grade.
          </div>
          <button className="btn-ghost !py-1.5 text-xs mt-3" onClick={() => uploadInput.current?.click()} disabled={!!busy}>
            <ImageUp className="h-3.5 w-3.5" /> Upload an image
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-3 gap-2.5 max-h-[56vh] overflow-y-auto pr-0.5">
          {rows.map((r) => (
            <button
              key={r.url}
              className="group text-left rounded-lg border border-border bg-raise/40 p-1.5 hover:border-zinc-500 hover:bg-raise transition-colors disabled:opacity-60"
              disabled={!!busy}
              onClick={() => pick(r.url, r.source)}
              title={`Use this image (${r.source})`}
            >
              <div className="relative aspect-square rounded-md overflow-hidden bg-panel">
                {/* Provider art, served by the app — see `api.artUrl`. The
                    candidate's own label is the artist name the backend's
                    fallback is asked about. */}
                <img
                  src={api.artUrl(r.url, { artist: r.label || artist })}
                  alt=""
                  loading="lazy"
                  className="h-full w-full object-cover"
                />
                {busy === r.url && (
                  <div className="absolute inset-0 bg-black/60 flex items-center justify-center">
                    <Loader2 className="h-5 w-5 animate-spin text-white" />
                  </div>
                )}
                <div className="absolute inset-0 opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity flex items-center justify-center bg-black/40">
                  <Check className="h-6 w-6 text-white" />
                </div>
              </div>
              <div className="mt-1.5 text-[11px] text-zinc-300 truncate" title={r.label}>
                {r.label}
              </div>
              <div className="text-[10px] text-zinc-600 truncate" title={`${r.source} · ${r.kind}`}>
                {r.source} · {r.kind}
              </div>
            </button>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2 mt-4">
        <input
          ref={uploadInput}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
        />
        <button className="btn-ghost !py-1.5 text-xs" onClick={() => uploadInput.current?.click()} disabled={!!busy}>
          <ImageUp className="h-3.5 w-3.5" /> {busy === "upload" ? "Uploading…" : "Upload from disk"}
        </button>
        <span className="text-[10px] text-zinc-600">
          Must be a real image (JPEG/PNG) — the artist folder keeps one file, artist.jpg.
        </span>
        <button className="btn-ghost !py-1.5 text-xs ml-auto" onClick={onClose}>
          Cancel
        </button>
      </div>
    </Modal>
  );
}
