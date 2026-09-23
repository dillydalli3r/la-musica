import { useRef, useState } from "react";
import type { DragEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AudioLines, Upload } from "lucide-react";
import { api } from "../api";
import type { ExportEqProfile } from "../api";
import { toast } from "../store";
import Modal from "./Modal";

/** The text of a profile FILE, decoded the three ways Windows tools write one:
 *  UTF-8 (with or without a BOM), UTF-16 — and, for anything else, the Windows
 *  code page the server's own reader falls back to. `File.text()` assumes
 *  UTF-8, so a UTF-16 Peace export read that way arrives as NUL-ridden garbage
 *  and would be refused for a reason that is not the user's. */
async function profileText(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const le = bytes.length > 1 && bytes[0] === 0xff && bytes[1] === 0xfe;
  const be = bytes.length > 1 && bytes[0] === 0xfe && bytes[1] === 0xff;
  if (le || be) return new TextDecoder(le ? "utf-16le" : "utf-16be").decode(bytes);
  const bom = bytes.length > 2 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
  if (bom) return new TextDecoder("utf-8").decode(bytes.subarray(3));
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return new TextDecoder("windows-1252").decode(bytes);
  }
}

/** Import one Equalizer APO / Peace profile — paste its text or drop the file
 *  Peace saved.
 *
 *  The profile is stored by the SERVER (<music>/.mlo/data/eq/<id>.txt), which
 *  also validates it: a band line it cannot read refuses the whole import with
 *  the line named, because a curve that silently loses one of its bands is a
 *  different curve. The result — how many bands, what was ignored, and every
 *  approximation the mapping to ffmpeg's filters makes — is shown here rather
 *  than assumed, and the imported profile is selected in the export menu on the
 *  way out. */
export default function EqProfileModal({ onClose, onImported }: {
  onClose: () => void;
  /** Called with the row the server stored. The PAGE opens this row rather
   *  than looking the id up in its own list: re-importing a name REPLACES that
   *  profile, so the list it holds carries the previous bands and opening
   *  those would put the old curve back in the editor (and let Save write it
   *  over the import). */
  onImported: (row: ExportEqProfile) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<ExportEqProfile | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const take = async (file: File) => {
    try {
      const body = await profileText(file);
      setText(body);
      setError("");
      // The file's own name is what the user will look for in the equalizer
      // list; it is only a suggestion, so an existing name is not overwritten
      // by it (the server decides whether a name is taken).
      if (!name.trim()) setName(file.name.replace(/\.[^.]+$/, ""));
    } catch (e) {
      setError(`could not read that file: ${String(e)}`);
    }
  };

  const drop = async (ev: DragEvent) => {
    ev.preventDefault();
    setDragging(false);
    const file = ev.dataTransfer.files?.[0];
    if (file) await take(file);
  };

  const save = async () => {
    if (!text.trim()) {
      setError("paste the profile's text or drop the file first");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const row = await api.exportEqImport(name.trim(), text);
      setSaved(row);
      onImported(row);
      qc.invalidateQueries({ queryKey: ["exportEq"] });
      toast.success(`Imported the equalizer profile “${row.label}”`);
    } catch (e) {
      // The server's sentence names the line it refused, which is the whole
      // point: show it verbatim.
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const bands = saved?.filters?.length ?? 0;

  return (
    <Modal
      title="Import an equalizer profile"
      subtitle="A profile Peace or Equalizer APO saved, pasted or dropped"
      icon={AudioLines}
      onClose={onClose}
      width="max-w-2xl"
      footer={
        <div className="flex items-center justify-end gap-2">
          <button className="btn text-xs tap" onClick={onClose} disabled={busy}>
            {saved ? "Done" : "Cancel"}
          </button>
          <button className="btn-primary text-xs tap" onClick={save} disabled={busy || !!saved}>
            <Upload className="h-3.5 w-3.5" />
            {busy ? "Importing…" : saved ? "Imported" : "Import profile"}
          </button>
        </div>
      }
    >
      <div className="space-y-3">
        <div className="text-[11px] text-zinc-500">
          The text an Equalizer APO / Peace profile is made of: <span className="text-zinc-300">Preamp:</span> and{" "}
          <span className="text-zinc-300">Filter N:</span> lines, a{" "}
          <span className="text-zinc-300">GraphicEQ:</span> band list, or Peace's own{" "}
          <span className="text-zinc-300">FilterCurve:</span> line. It is stored on the server, at{" "}
          <span className="text-zinc-400">{"<music folder>/.mlo/data/eq/"}</span>, and appears in the
          equalizer list on the export form.
        </div>

        <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
          Profile name
          <input
            className="input !py-1 text-xs"
            data-autofocus
            value={name}
            placeholder="Earbuds, car stereo, headphones…"
            onChange={(ev) => setName(ev.target.value)}
          />
        </label>

        <div
          className={`rounded-md border ${dragging ? "border-sky-500/60 bg-sky-950/20" : "border-border"} p-2`}
          onDragOver={(ev) => {
            ev.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={drop}
        >
          <textarea
            className="input text-xs font-mono h-40 resize-y"
            value={text}
            placeholder={'Preamp: -6.5 dB\nFilter 1: ON PK Fc 100 Hz Gain 2.5 dB Q 1.4'}
            onChange={(ev) => setText(ev.target.value)}
          />
          <div className="flex flex-wrap items-center gap-2 mt-2">
            <button className="btn-ghost text-xs tap" onClick={() => fileInput.current?.click()}>
              <Upload className="h-3 w-3" /> Choose a file…
            </button>
            <span className="text-[10px] text-zinc-600">or drop it on the box above</span>
            <input
              ref={fileInput}
              type="file"
              accept=".txt,text/plain"
              className="hidden"
              onChange={(ev) => {
                const file = ev.target.files?.[0];
                if (file) void take(file);
                ev.target.value = "";
              }}
            />
          </div>
        </div>

        {error && (
          <div className="text-[11px] text-red-300 whitespace-pre-wrap">{error}</div>
        )}

        {saved && (
          <div className="rounded-md border border-border p-2 space-y-1">
            <div className="text-[11px] text-zinc-300">
              <span className="font-semibold">{saved.label}</span> — {bands} band{bands === 1 ? "" : "s"}
              {saved.preamp_db ? `, preamp ${saved.preamp_db} dB` : ""}
              {saved.empty ? " — no filters in this file" : ""}
            </div>
            {/* What the mapping to ffmpeg's filters does NOT reproduce is the
                part a user has to be told, so the server's own words are shown
                here rather than left in the response. */}
            {(saved.notes ?? []).map((n) => (
              <div key={n} className="text-[10px] text-zinc-500">{n}</div>
            ))}
            {(saved.unsupported ?? []).length > 0 && (
              <div className="text-[10px] text-amber-300/90">
                ignored, with no effect on the curve: {(saved.unsupported ?? []).join(" · ")}
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
