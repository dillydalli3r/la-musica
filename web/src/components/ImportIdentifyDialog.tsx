import { useState } from "react";
import { Disc3, Fingerprint, Loader2, Search } from "lucide-react";
import Modal from "./Modal";
import { api } from "../api";
import { toast } from "../store";
import type { AcoustidAlbumMatch } from "../types";

/** One album the import dialog asks about: the release it names (prefilled in
 *  its row, and what a row left alone keeps) or nothing, when its files name
 *  no release at all. */
export interface IdentifyAlbum {
  path: string;
  artist: string;
  album: string;
  /** The release id the album's files already carry, when they carry one.
   *  Prefills the row and is what an untouched row keeps. */
  current?: string;
}

/** "Which release is this?" — the one question the import pipeline cannot
 *  answer for itself.
 *
 *  The owner's ask: an import must not be a guess. That is what it was — the
 *  audio is matched by name (or not at all), so an album like Toxicity could
 *  take another pressing's identity and cover and quietly import the wrong
 *  thing. Every album in the selection gets a row here, and each row offers
 *  the three ways a person actually knows which release it is:
 *
 *  * paste a MusicBrainz link (or the bare ID) — a release URL, a release-group
 *    URL, either is accepted and the server resolves it;
 *  * **Detect** — fingerprint the album with AcoustID and let it say which
 *    release GROUP the audio is, with its own score and the tracks it matched
 *    (the app's own AcoustID path: `POST /api/import/acoustid`);
 *  * **catalog number** — type the CD's catalogue number and MusicBrainz's own
 *    `catno:` search comes back with the releases that carry it, each one click
 *    from being the pin.
 *
 *  The row IS the release change: a row opens on the release the album's files
 *  name now, and editing it re-points that album — the pipeline stamps a pin
 *  OVER an existing id (`force_ids`, `server.imports._stamp_release`), so the
 *  release id, the metadata and the tracklist are rewritten from what the row
 *  names. Only a row the user actually edits is sent as a pin, so a selection
 *  with nothing changed imports exactly as it would have. An album left blank
 *  is imported the way it always was (matched by name): the dialog is a chance
 *  to be sure, never a wall. */
export default function ImportIdentifyDialog({
  albums,
  onClose,
  onStart,
}: {
  albums: IdentifyAlbum[];
  onClose: () => void;
  onStart: (pins: Record<string, string>) => void | Promise<void>;
}) {
  const [pins, setPins] = useState<Record<string, string>>({});
  const [catno, setCatno] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, "detect" | "find" | "start">>({});
  const [detected, setDetected] = useState<Record<string, AcoustidAlbumMatch>>({});
  const [found, setFound] = useState<Record<string, any[]>>({});

  const setPin = (path: string, value: string) =>
    setPins((cur) => ({ ...cur, [path]: value }));

  const detect = async (al: IdentifyAlbum) => {
    setBusy((b) => ({ ...b, [al.path]: "detect" }));
    try {
      const res = await api.importAcoustid([al.path]);
      const row = res.albums?.[0];
      if (!row) {
        toast("AcoustID: nothing came back for that album", "error");
        return;
      }
      setDetected((cur) => ({ ...cur, [al.path]: row }));
      if (!res.available) toast(res.note || "AcoustID is not configured", "error");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(({ [al.path]: _gone, ...rest }) => rest);
    }
  };

  const find = async (al: IdentifyAlbum) => {
    const q = (catno[al.path] || "").trim();
    if (!q) {
      toast("Type the release's catalogue number first");
      return;
    }
    setBusy((b) => ({ ...b, [al.path]: "find" }));
    try {
      const rows = await api.mbSearchReleases(q, "catno");
      setFound((cur) => ({ ...cur, [al.path]: rows || [] }));
      if (!rows || !rows.length) toast(`No MusicBrainz release carries "${q}"`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(({ [al.path]: _gone, ...rest }) => rest);
    }
  };

  const start = async () => {
    const pinsOut: Record<string, string> = {};
    for (const [path, value] of Object.entries(pins)) {
      const v = (value || "").trim();
      // A row left on the release it already names sends NO pin: an unchanged
      // album re-imports the way it always did, and a pin — which the pipeline
      // stamps over an existing id — is reserved for the row the user actually
      // re-pointed.
      const current = (albums.find((a) => a.path === path)?.current || "").trim();
      if (v && v.toLowerCase() !== current.toLowerCase()) pinsOut[path] = v;
    }
    setBusy({ start: "start" });
    try {
      await onStart(pinsOut);
    } finally {
      setBusy({});
    }
  };

  // "pinned" is what the press will SEND: a row still holding the release it
  // already names is not a pin (see `start`), so the count the subtitle and
  // the button print is the rows the user actually re-pointed.
  const pinned = albums.filter((a) => {
    const v = (pins[a.path] || "").trim();
    return !!v && v.toLowerCase() !== (a.current || "").trim().toLowerCase();
  }).length;
  // How many rows open on a release of their own: the wording below turns on
  // it, because "keeps the release it names" says nothing to a user whose
  // rows are all blank (nothing named yet).
  const named = albums.filter((a) => (a.current || "").trim()).length;
  const keptNote = named
    ? "A row left alone keeps the release it already names."
    : "An album left blank is imported the way it always was — matched by name.";
  const pending = Object.keys(busy).length > 0;

  return (
    <Modal
      icon={Disc3}
      title={albums.length === 1 ? "Which release is this?" : `Releases — ${albums.length} albums`}
      subtitle={
        `${named
          ? `${named} of these already name their release in the files, and each row opens on it — change a link and that album is re-pointed at the release you name (its release id, metadata and tracklist are rewritten from it). `
          : ""}${keptNote}`
        + (pinned ? ` · ${pinned} re-pointed` : "")
      }
      onClose={onClose}
      width="max-w-3xl"
      bodyClass="px-5 py-4 space-y-3"
      footer={
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-zinc-500">{keptNote}</span>
          <span className="ml-auto inline-flex items-center gap-2">
            <button className="btn-ghost !py-1.5 text-xs tap" onClick={onClose} disabled={pending}>
              Cancel
            </button>
            <button className="btn-primary !py-1.5 text-xs tap" onClick={() => void start()} disabled={pending}>
              {busy.start ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Import {albums.length} album{albums.length === 1 ? "" : "s"}
              {pinned ? ` — ${pinned} ${named ? "re-pointed" : "pinned"}` : ""}
            </button>
          </span>
        </div>
      }
    >
      {albums.map((al) => {
        const det = detected[al.path];
        const rows = found[al.path];
        return (
          <div key={al.path} className="rounded-lg border border-border bg-panel/60 p-3 space-y-2">
            <div className="flex items-baseline gap-2 flex-wrap">
              <Disc3 className="h-3.5 w-3.5 text-zinc-500" />
              <span className="text-sm text-zinc-100">{al.album}</span>
              <span className="text-xs text-zinc-500">{al.artist}</span>
              {al.path && (
                <span className="text-[10px] text-zinc-600 font-mono truncate max-w-[18rem]" title={al.path}>
                  {al.path.split(/[\\/]/).slice(-2).join("/")}
                </span>
              )}
            </div>
            <input
              className="input !py-1.5 text-xs font-mono"
              placeholder="MusicBrainz release or release-group URL, or the bare ID"
              value={pins[al.path] ?? al.current ?? ""}
              onChange={(e) => setPin(al.path, e.target.value)}
            />
            <div className="flex flex-wrap items-center gap-2">
              <button
                className="btn-ghost !py-1 text-xs tap"
                onClick={() => void detect(al)}
                disabled={pending}
                title="Fingerprint this album with AcoustID and let it say which release group the audio is"
              >
                {busy[al.path] === "detect"
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <Fingerprint className="h-3.5 w-3.5" />}
                Detect (AcoustID)
              </button>
              <span className="inline-flex items-center gap-1.5 ml-auto">
                <input
                  className="input !py-1 !w-40 text-xs"
                  placeholder="Catalogue number"
                  value={catno[al.path] ?? ""}
                  onChange={(e) => setCatno((c) => ({ ...c, [al.path]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void find(al);
                    }
                  }}
                />
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => void find(al)}
                  disabled={pending}
                  title="Search MusicBrainz for the release with this catalogue number"
                >
                  {busy[al.path] === "find"
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <Search className="h-3.5 w-3.5" />}
                  Find
                </button>
              </span>
            </div>
            {det && (
              <p className="text-[11px] leading-relaxed text-zinc-400">
                {det.status === "matched" && det.release_group_id ? (
                  <>
                    AcoustID: <b className="text-zinc-200">{det.release_group_title || "a release group"}</b>
                    {det.artists?.length ? ` — ${det.artists.join(", ")}` : ""}
                    {typeof det.score === "number" ? ` · score ${det.score.toFixed(2)}` : ""}
                    {det.total ? ` · ${det.matched ?? 0}/${det.total} tracks` : ""}
                    {" · "}
                    <button
                      className="text-accent-soft hover:text-white underline underline-offset-2"
                      onClick={() =>
                        setPin(al.path, `https://musicbrainz.org/release-group/${det.release_group_id}`)
                      }
                    >
                      use this
                    </button>
                  </>
                ) : (
                  <>AcoustID: {det.reason || det.status || "no match"}</>
                )}
              </p>
            )}
            {rows && rows.length > 0 && (
              <ul className="space-y-1">
                {rows.slice(0, 5).map((r) => (
                  <li key={String(r.id)} className="flex items-baseline gap-2 text-[11px]">
                    <button
                      className="text-left text-zinc-300 hover:text-white underline-offset-2 hover:underline truncate"
                      onClick={() => setPin(al.path, `https://musicbrainz.org/release/${r.id}`)}
                      title="Use this release"
                    >
                      {r.title || r.id}
                    </button>
                    <span className="text-zinc-500 truncate">
                      {[r.artist, r.date, r.country].filter(Boolean).join(" · ")}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </Modal>
  );
}
