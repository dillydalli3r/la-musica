import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Check, ExternalLink, Link2, Loader2 } from "lucide-react";
import Popover from "./Popover";
import { api } from "../api";
import { toast } from "../store";
import mbLogo from "../assets/musicbrainz.png";

/* MusicBrainz identity links.
 *
 * LinkChips renders the open-in-database icon row from a tags object;
 * LinkEditorButton opens the paste-a-URL editor that maps a pasted
 * musicbrainz.org link (or bare MBID) onto the right tags and writes them to
 * every given path via /api/mb/assign. */

const MBID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Pull a bare MBID out of a pasted URL (any entity) or accept a bare ID. */
function mbidFrom(input: string): string | null {
  const t = input.trim();
  if (!t) return null;
  const fromUrl = /musicbrainz\.org\/[a-z-]+\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(t);
  if (fromUrl) return fromUrl[1].toLowerCase();
  if (MBID_RE.test(t)) return t.toLowerCase();
  return null;
}

interface FieldDef {
  key: string; // tag written
  label: string;
  placeholder: string;
}

const FIELDS: Record<"artist" | "album" | "track", FieldDef[]> = {
  artist: [
    { key: "MUSICBRAINZ_ARTISTID", label: "MusicBrainz artist", placeholder: "musicbrainz.org/artist/… or MBID" },
  ],
  album: [
    { key: "MUSICBRAINZ_ALBUMID", label: "MusicBrainz release", placeholder: "musicbrainz.org/release/… or MBID" },
    { key: "MUSICBRAINZ_RELEASEGROUPID", label: "MusicBrainz release group", placeholder: "musicbrainz.org/release-group/… or MBID" },
  ],
  track: [
    { key: "MUSICBRAINZ_TRACKID", label: "MusicBrainz recording", placeholder: "musicbrainz.org/recording/… or MBID" },
  ],
};

const MB_URL: Record<string, (v: string) => string> = {
  MUSICBRAINZ_ARTISTID: (v) => `https://musicbrainz.org/artist/${v}`,
  MUSICBRAINZ_ALBUMID: (v) => `https://musicbrainz.org/release/${v}`,
  MUSICBRAINZ_RELEASEGROUPID: (v) => `https://musicbrainz.org/release-group/${v}`,
  MUSICBRAINZ_TRACKID: (v) => `https://musicbrainz.org/recording/${v}`,
  MUSICBRAINZ_ALBUMARTISTID: (v) => `https://musicbrainz.org/artist/${v}`,
};

/** Official MusicBrainz mark (2016 hexagon logo). */
export function MbIcon({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <img
      src={mbLogo}
      alt=""
      aria-hidden
      draggable={false}
      className={`${className} object-contain select-none`}
    />
  );
}

/** Icon row opening every MusicBrainz link present in `tags`.
 *
 * `only` narrows the row to specific tags (and prefers the first listed):
 * album pages pass their album-level tags so exactly one MusicBrainz link
 * shows, even when a release-group ID also exists. */
export function LinkChips({ tags, only }: { tags: Record<string, unknown>; only?: string[] }) {
  let mbEntries = Object.entries(MB_URL).filter(([tag]) => {
    const v = tags?.[tag];
    return typeof v === "string" && !!v.trim();
  });
  if (only?.length) {
    const rank = new Map(only.map((t, i) => [t, i]));
    mbEntries = mbEntries
      .filter(([tag]) => rank.has(tag))
      .sort((a, b) => (rank.get(a[0]) ?? 99) - (rank.get(b[0]) ?? 99))
      .slice(0, 1); // exactly one MusicBrainz link
  }
  if (!mbEntries.length) return null;
  return (
    <span className="inline-flex items-center gap-1">
      {mbEntries.map(([tag, build], i) => (
        <a
          key={i}
          href={build(tags[tag] as string)}
          target="_blank"
          rel="noreferrer"
          title={`Open MusicBrainz ${tag}`}
          className="p-1.5 rounded-lg hover:bg-raise transition-transform hover:scale-110 inline-flex items-center"
        >
          <MbIcon className="h-4 w-4" />
        </a>
      ))}
    </span>
  );
}

/** The one chip every link field shows: Valid, or why the paste is not the
 *  page that field wants.
 *
 *  Used by this file's link editor, so the MusicBrainz IDs are worded
 *  identically wherever they are entered. */
export function LinkValidChip({ state }: { state: boolean | null }) {
  if (state === true) {
    return (
      <span className="chip bg-emerald-900/60 text-emerald-300 border border-emerald-800 shrink-0">
        <Check className="h-3 w-3" /> Valid
      </span>
    );
  }
  if (state !== false) return null;
  return (
    <span
      className="chip bg-red-900/50 text-red-300 border border-red-900 shrink-0"
      title="Paste a musicbrainz.org link or a bare MBID"
    >
      Not a MusicBrainz link or ID
    </span>
  );
}

/** "Links" button + popover editor. Writes go to EVERY path given, which is
 * what makes album/artist level linking one paste per field. */
export function LinkEditorButton({
  mode,
  paths,
  current,
  iconOnly,
  onSaved,
}: {
  mode: "artist" | "album" | "track";
  paths: string[];
  current?: Record<string, unknown>;
  iconOnly?: boolean;
  onSaved?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();

  // Validate each field as it is typed: a MusicBrainz field needs no round
  // trip — the MBID is what makes it valid.
  const [checks, setChecks] = useState<Record<string, { valid: boolean }>>({});
  useEffect(() => {
    const out: Record<string, { valid: boolean }> = {};
    for (const f of FIELDS[mode]) {
      const raw = (values[f.key] ?? "").trim();
      if (!raw) continue;
      out[f.key] = { valid: !!mbidFrom(raw) };
    }
    setChecks(out);
  }, [values, mode]);

  const save = async () => {
    const writes: Record<string, Record<string, string>> = {};
    for (const f of FIELDS[mode]) {
      const raw = (values[f.key] ?? "").trim();
      if (!raw) continue;
      const id = mbidFrom(raw);
      if (!id) {
        toast(`${f.label}: not a MusicBrainz URL or ID`);
        return;
      }
      for (const p of paths) (writes[p] ??= {})[f.key] = id;
    }
    if (!Object.keys(writes).length) {
      toast("Nothing to save");
      return;
    }
    setBusy(true);
    try {
      await api.mbAssign(writes);
      toast(`Links written to ${paths.length} file(s)`);
      setValues({});
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["tags"] });
      qc.invalidateQueries({ queryKey: ["album"] });
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["artist"] });
      onSaved?.();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative">
      <button
        className={iconOnly ? "btn-icon" : "btn-ghost !py-1.5 text-xs"}
        onClick={() => setOpen(!open)}
        title="Paste MusicBrainz links"
        aria-label="Links"
      >
        <Link2 className="h-4 w-4" />
        {!iconOnly && " Links"}
      </button>
      <Popover open={open} onClose={() => setOpen(false)} panelClass="w-80 p-3 space-y-2.5">
            <div className="text-[10px] uppercase tracking-wider text-zinc-500">
              Identity links · {mode} level
            </div>
            {FIELDS[mode].map((f) => {
              const cur = current?.[f.key];
              return (
                <div key={f.key}>
                  <label className="text-[11px] text-zinc-400 flex items-center gap-1.5">
                    {f.label}
                    {typeof cur === "string" && cur && (
                      <a
                        href={MBID_RE.test(cur) ? MB_URL[f.key]?.(cur) : cur}
                        target="_blank"
                        rel="noreferrer"
                        className="text-accent-soft hover:underline inline-flex items-center gap-0.5"
                        title="Open the current link"
                      >
                        set <ExternalLink className="h-2.5 w-2.5" />
                      </a>
                    )}
                  </label>
                  <div className="flex items-center gap-1.5 mt-0.5">
                    <input
                      className={`input !py-1 !px-2 text-xs flex-1 tap ${
                        checks[f.key]?.valid === true
                          ? "!border-emerald-700"
                          : checks[f.key]?.valid === false
                            ? "!border-red-800"
                            : ""
                      }`}
                      placeholder={f.placeholder}
                      value={values[f.key] ?? ""}
                      onChange={(e) => {
                        const v = e.target.value;
                        setValues((s) => ({ ...s, [f.key]: v }));
                      }}
                    />
                    <LinkValidChip state={checks[f.key]?.valid ?? null} />
                  </div>
                </div>
              );
            })}
            <div className="flex justify-end gap-1.5 pt-1">
              <button className="btn-ghost !py-1 text-xs tap" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button className="btn-primary !py-1 text-xs tap" onClick={save} disabled={busy}>
                {busy && <Loader2 className="h-3 w-3 animate-spin" />} Write to {paths.length} file(s)
              </button>
            </div>
            <div className="text-[10px] text-zinc-600">
              Paste links (or bare MBIDs). Values are written as real tags so grading sees them.
            </div>
      </Popover>
    </div>
  );
}