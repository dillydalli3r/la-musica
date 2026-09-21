import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Image as ImageIcon, BadgeInfo, Loader2, RefreshCw, Trash2, User, Disc3 } from "lucide-react";
import { api, answerSources, checkTrackValues, replyFor } from "../api";
import type { AdvisoryFetchResult, InstrumentalFetchResult } from "../api";
import Modal from "./Modal";
import { advisoryLine, advisoryOutcome, instrumentalLine } from "./Badges";
import { toast } from "../store";

/** Provider id → the name a reader knows ("audiodb" → TheAudioDB). */
const SOURCE_NAMES: Record<string, string> = {
  wikipedia: "Wikipedia",
  lastfm: "Last.fm",
  discogs: "Discogs",
  musicbrainz: "MusicBrainz",
  deezer: "Deezer",
  itunes: "Apple Music",
  audiodb: "TheAudioDB",
  listenbrainz: "ListenBrainz",
  upload: "Uploaded",
  manual: "Manual",
};

/** "Wikipedia · fetched 2 min ago" — provenance the review needs to judge a
 *  candidate without trusting it blindly. */
function provenance(source?: string | null, fetched?: string | null) {
  const label = source ? SOURCE_NAMES[source] ?? source : "unknown source";
  if (!fetched) return label;
  const when = new Date(fetched);
  if (Number.isNaN(when.getTime())) return label;
  const mins = Math.round((Date.now() - when.getTime()) / 60000);
  const ago = mins < 1 ? "just now" : mins < 60 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`;
  return `${label} · fetched ${ago}`;
}

/** Metadata review: the candidate artist images as a grid plus the artist and
 *  album descriptions, each labelled with its source and fetch time, each with
 *  Apply / Clear. Opened from the artist/album pages, the shared tag-actions
 *  menu, and (when `metadata_review` is on) the import flow. */
export default function MetadataReviewModal({
  artist,
  albumPath,
  paths = [],
  title,
  onClose,
  onSaved,
}: {
  /** Artist folder path or name — required for the image + artist description. */
  artist?: string;
  /** Album folder — required for the album description. */
  albumPath?: string;
  /** The album's tracks: with them the modal also reviews the per-track
   *  advisory + instrumental values and the sources that stated them. */
  paths?: string[];
  title?: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["metadataCandidates", artist ?? "", albumPath ?? ""],
    queryFn: () => api.metadataCandidates(artist ?? "", albumPath),
    enabled: !!artist || !!albumPath,
    retry: false,
  });
  const [busy, setBusy] = useState<string | null>(null);

  // The per-track check WRITES the values the sources state and reports who
  // stated each one — that reply is the only place the provenance exists, so
  // it is fetched on demand and never inferred from the stored tag.
  const [checked, setChecked] = useState<null | { adv?: AdvisoryFetchResult; inst?: InstrumentalFetchResult }>(null);
  const [checking, setChecking] = useState(false);
  /** `force` is the advisory re-rate: the server echoes a file that already
   *  carries a valid 0/1/2, and forcing asks anyway and writes what the sources
   *  state — the only route that can lower a rating. */
  const checkPerTrack = async (force = false) => {
    setChecking(true);
    try {
      const { adv, inst, errors } = await checkTrackValues(paths, force);
      setChecked({ adv: adv ?? undefined, inst: inst ?? undefined });
      if (errors.length) toast.error(errors.join(" · "));
      else toast(`Checked — ${advisoryOutcome(adv)}, ${inst?.updated ?? 0} instrumental value(s) written`);
      onSaved?.();
    } finally {
      setChecking(false);
    }
  };
  /** The re-rate, behind a confirmation: it can LOWER a rating, so a stray
   *  click must not reach it. */
  const reRatePerTrack = () => {
    if (
      !window.confirm(
        `Re-rate ITUNESADVISORY for ${paths.length} track(s)?\n\n` +
          "This asks even for files that already carry a value, and a source's answer can lower a rating (1 → 0)."
      )
    )
      return;
    void checkPerTrack(true);
  };

  const run = async (key: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await fn();
      toast.success(done);
      onSaved?.();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const images = data?.images ?? [];
  const artistDesc = data?.artist_description ?? null;
  const albumDesc = data?.album_description ?? null;

  return (
    <Modal
      onClose={onClose}
      title={title ?? "Metadata review"}
      subtitle="Pick a candidate, or clear what is stored — provenance is shown for each."
      width="max-w-4xl"
      bodyClass="px-5 py-4 space-y-5"
      headerExtra={
        <button
          className="p-1.5 rounded-lg hover:bg-raise text-zinc-500 hover:text-white shrink-0"
          onClick={() => refetch()}
          disabled={isFetching}
          title="Ask the providers again"
        >
          <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
        </button>
      }
    >
      {isLoading ? (
        <div className="flex items-center justify-center gap-2 py-12 text-xs text-zinc-500">
          <Loader2 className="h-4 w-4 animate-spin" /> Asking the metadata providers…
        </div>
      ) : error ? (
        <p className="py-8 text-center text-xs text-red-300">
          {String(error instanceof Error ? error.message : error)}
        </p>
      ) : (
        <>
          {artist && (
            <section className="space-y-2">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
                <ImageIcon className="h-3.5 w-3.5" /> Artist images
                <span className="text-zinc-600 normal-case font-normal">
                  {images.length ? `${images.length} candidate${images.length === 1 ? "" : "s"}` : "none found"}
                </span>
              </div>
              {images.length > 0 && (
                <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(112px, 1fr))" }}>
                  {images.map((img) => (
                    <button
                      key={img.url}
                      className="group relative overflow-hidden rounded-lg border border-border hover:border-accent transition-colors"
                      disabled={busy === img.url}
                      title={`${SOURCE_NAMES[img.source] ?? img.source}${img.width ? ` · ${img.width}×${img.height}` : ""}`}
                      onClick={() =>
                        run(
                          img.url,
                          () => api.metadataApply({ kind: "artist_image", artist, image_url: img.url }),
                          "Artist image saved"
                        )
                      }
                    >
                      <img
                        src={api.artUrl(img.url, { artist })}
                        alt=""
                        className="h-28 w-full object-cover"
                        loading="lazy"
                      />
                      <span className="absolute inset-x-0 bottom-0 bg-black/70 px-1.5 py-1 text-[9px] text-zinc-300 truncate">
                        {SOURCE_NAMES[img.source] ?? img.source}
                      </span>
                      {busy === img.url && (
                        <span className="absolute inset-0 flex items-center justify-center bg-black/60">
                          <Loader2 className="h-4 w-4 animate-spin text-white" />
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </section>
          )}

          {artist && (
            <section className="space-y-2">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
                <User className="h-3.5 w-3.5" /> Artist description
                {artistDesc && <span className="text-zinc-600 normal-case font-normal">{provenance(artistDesc.source, artistDesc.fetched)}</span>}
              </div>
              {artistDesc ? (
                <>
                  <p className="text-xs text-zinc-300 leading-relaxed whitespace-pre-line max-h-40 overflow-y-auto bg-raise/40 border border-border rounded-lg p-3">
                    {artistDesc.text}
                  </p>
                  <div className="flex items-center gap-2">
                    <button
                      className="btn-primary !py-1 text-xs"
                      disabled={busy === "artist-desc-apply"}
                      onClick={() =>
                        run(
                          "artist-desc-apply",
                          () =>
                            api.metadataApply({
                              kind: "artist_description",
                              artist,
                              description: artistDesc.text,
                            }),
                          "Artist description saved"
                        )
                      }
                    >
                      {busy === "artist-desc-apply" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Apply
                    </button>
                    <button
                      className="btn-ghost !py-1 text-xs text-red-300/80 hover:text-red-200"
                      disabled={busy === "artist-desc-clear"}
                      onClick={() => run("artist-desc-clear", () => api.artistDescriptionClear(artist), "Artist description cleared")}
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Clear stored
                    </button>
                  </div>
                </>
              ) : (
                <p className="text-xs text-zinc-500">No description candidate from the configured sources.</p>
              )}
            </section>
          )}

          {albumPath && (
            <section className="space-y-2">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
                <Disc3 className="h-3.5 w-3.5" /> Album description
                {albumDesc && <span className="text-zinc-600 normal-case font-normal">{provenance(albumDesc.source, albumDesc.fetched)}</span>}
              </div>
              {albumDesc ? (
                <>
                  <p className="text-xs text-zinc-300 leading-relaxed whitespace-pre-line max-h-40 overflow-y-auto bg-raise/40 border border-border rounded-lg p-3">
                    {albumDesc.text}
                  </p>
                  <div className="flex items-center gap-2">
                    <button
                      className="btn-primary !py-1 text-xs"
                      disabled={busy === "album-desc-apply"}
                      onClick={() =>
                        run(
                          "album-desc-apply",
                          () =>
                            api.metadataApply({
                              kind: "album_description",
                              album_path: albumPath,
                              description: albumDesc.text,
                            }),
                          "Album description saved"
                        )
                      }
                    >
                      {busy === "album-desc-apply" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Apply
                    </button>
                    <button
                      className="btn-ghost !py-1 text-xs text-red-300/80 hover:text-red-200"
                      disabled={busy === "album-desc-clear"}
                      onClick={() => run("album-desc-clear", () => api.albumDescriptionClear(albumPath), "Album description cleared")}
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Clear stored
                    </button>
                  </div>
                </>
              ) : (
                <p className="text-xs text-zinc-500">No description candidate from the configured sources.</p>
              )}
            </section>
          )}

          {paths.length > 0 && (
            <section className="space-y-2">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
                <BadgeInfo className="h-3.5 w-3.5" /> Per-track advisory & instrumental
                <span className="text-zinc-600 normal-case font-normal">
                  {checked
                    ? `${paths.length} track${paths.length === 1 ? "" : "s"} checked`
                    : "not checked in this pass"}
                </span>
                <button
                  className="btn-ghost !py-0.5 !px-1.5 ml-auto normal-case tracking-normal text-[10px] font-normal"
                  onClick={() => checkPerTrack(false)}
                  disabled={checking}
                  title="Ask the configured sources for each track's advisory + INSTRUMENTAL and write what they state. A track that already carries a value keeps it — Re-rate asks anyway."
                >
                  {checking ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                  Check
                </button>
                <button
                  className="btn-ghost !py-0.5 !px-1.5 normal-case tracking-normal text-[10px] font-normal"
                  onClick={reRatePerTrack}
                  disabled={checking}
                  title="Ask the advisory sources again even for tracks that already carry a value, and write what they state — the only way a rating can go down"
                >
                  Re-rate…
                </button>
              </div>
              {checked ? (
                <div className="rounded-md border border-border overflow-hidden">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-zinc-500">
                        <th className="px-2 py-1 font-medium">Track</th>
                        <th className="px-2 py-1 font-medium">Advisory</th>
                        <th className="px-2 py-1 font-medium">Instrumental</th>
                      </tr>
                    </thead>
                    <tbody>
                      {paths.map((p) => (
                        <tr key={p} className="border-t border-border">
                          <td className="px-2 py-1 text-zinc-300 truncate max-w-[14rem]" title={p}>
                            {p.split(/[\\/]/).pop()}
                          </td>
                          <td className="px-2 py-1 text-zinc-300">
                            {/* The value's provenance and what this run did
                                with it both live in the reply: a re-check the
                                sources agreed with and a gate refusal are not
                                writes, and the number alone cannot say so. */}
                            {advisoryLine(
                              replyFor(checked.adv?.values, p),
                              answerSources(replyFor(checked.adv?.answers, p), replyFor(checked.adv?.sources, p)),
                              replyFor(checked.adv?.status, p)
                            )}
                          </td>
                          <td className="px-2 py-1 text-zinc-300">
                            {instrumentalLine(
                              replyFor(checked.inst?.values, p),
                              answerSources(replyFor(checked.inst?.evidence, p))
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-zinc-500">
                  Nothing checked yet — Check asks the advisory and instrumental sources for these {paths.length} track(s)
                  and lists every value it fetched with the source behind it.
                </p>
              )}
            </section>
          )}
        </>
      )}
    </Modal>
  );
}
