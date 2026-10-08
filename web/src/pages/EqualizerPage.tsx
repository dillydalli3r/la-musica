import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download, Headphones, Loader2, Plus, RotateCcw, Save, Trash2, Upload, X,
} from "lucide-react";
import { api, type EqBand, type ExportEqProfile } from "../api";
import PageHeader from "../components/PageHeader";
import EqCurve from "../components/EqCurve";
import EqProfileModal from "../components/EqProfileModal";
import EqAutoEqDialog from "../components/EqAutoEqDialog";
import ConfirmButton from "../components/ConfirmButton";
import { PageLoading } from "../components/Badges";
import {
  EQ_GAIN_LIMIT, EQ_NEW_BAND, EQ_PREAMP_LIMIT, EQ_TYPES, eqToApoText, eqType,
} from "../lib/eqNodes";
import { toast } from "../store";

/** What the editor holds: a profile's bands, copied out so an edit never
 *  writes through to the stored file until Save is pressed. */
type Draft = {
  /** The profile this was loaded from ("" while a fresh, unsaved curve). */
  sourceId: string;
  name: string;
  preampDb: number;
  filters: EqBand[];
};

const SHAPE_ONLY = new Set(["LP", "HP", "BP", "NO"]);

/** The empty catalogue slice, as ONE value: `?? []` would hand `useMemo` (and
 *  every effect keyed on the profile list) a fresh array identity on each
 *  render, which rebuilds the list and re-runs them for nothing. */
const NO_PROFILES: ExportEqProfile[] = [];

const num = (v: unknown, digits = 1): string => {
  const n = Number(v);
  if (!Number.isFinite(n)) return "0";
  return String(Number(n.toFixed(digits)));
};

/** A typeable number box with a unit, committed on Enter or blur. Typing "50"
 *  must not be read as 5 mid-keystroke, and a value that lands outside the range
 *  is clamped to the range the server and the renderer both honour rather than
 *  refused. */
function NumField({
  value, onCommit, min, max, digits = 1, suffix, disabled, title, width = "w-16",
}: {
  value: number;
  onCommit: (v: number) => void;
  min: number;
  max: number;
  digits?: number;
  suffix?: string;
  disabled?: boolean;
  title?: string;
  width?: string;
}) {
  const [text, setText] = useState(() => num(value, digits));
  const focused = useRef(false);
  // Escape blurs the box on purpose, and the blur runs `onBlur` — which would
  // commit the very text Escape is throwing away. The flag is what the blur
  // reads to know the value was already restored.
  const reverted = useRef(false);
  // A drag on the curve rewrites the value behind this box: reflect it, but
  // never while the user is typing into it.
  useEffect(() => {
    if (!focused.current) setText(num(value, digits));
  }, [value, digits]);

  const commit = () => {
    const n = Number(text.replace(",", ".").trim());
    if (!text.trim() || !Number.isFinite(n)) {
      setText(num(value, digits));
      return;
    }
    const clamped = Math.max(min, Math.min(max, n));
    setText(num(clamped, digits));
    if (clamped !== value) onCommit(clamped);
  };

  return (
    <span className="inline-flex items-center gap-0.5">
      <input
        className={`${width} bg-transparent border border-border/60 hover:border-border focus:border-accent rounded px-1 py-0.5 text-right text-[11px] font-mono tabular-nums outline-none disabled:opacity-40`}
        inputMode="decimal"
        value={text}
        disabled={disabled}
        title={title}
        onFocus={() => { focused.current = true; }}
        onChange={(e) => setText(e.target.value.replace(/[^\d.,+-]/g, ""))}
        onBlur={() => {
          focused.current = false;
          if (reverted.current) { reverted.current = false; return; }
          commit();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            commit();
            (e.target as HTMLInputElement).blur();
          } else if (e.key === "Escape") {
            reverted.current = true;
            setText(num(value, digits));
            (e.target as HTMLInputElement).blur();
          }
        }}
      />
      {suffix ? <span className="text-[10px] text-zinc-500">{suffix}</span> : null}
    </span>
  );
}

/** The Equalizer: pick or build an Equalizer APO / Peace profile and edit its
 *  curve — a configuration surface for the Export page's EQ bake.
 *
 *  The profiles here are the profiles the Export page bakes into a transcode —
 *  one parser (mlo.eq), one set of files in `<music>/.mlo/data/eq`, so a curve
 *  the listener likes can be exported without being retyped, and an AutoEq
 *  import or a pasted Peace file is stored the same way. Nothing on this page
 *  writes a second kind of profile. */
export default function EqualizerPage() {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["exportEq"], queryFn: api.exportEq });

  const [draft, setDraft] = useState<Draft | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [autoEqOpen, setAutoEqOpen] = useState(false);
  const [saveAsName, setSaveAsName] = useState("");
  const [busy, setBusy] = useState(false);
  // A profile imported from a dialog exists on the server before this page's
  // catalogue query knows about it: the id waits here until the refreshed list
  // carries it, and then it is opened.

  const presets = catalog.data?.presets ?? NO_PROFILES;
  const profiles = catalog.data?.profiles ?? NO_PROFILES;
  const all = useMemo(() => [...presets, ...profiles], [presets, profiles]);

  const load = useCallback((row: ExportEqProfile) => {
    setDraft({
      sourceId: row.id,
      name: row.label,
      preampDb: Number(row.preamp_db) || 0,
      filters: (row.filters ?? []).map((f) => ({
        type: String(f.type || "PK"),
        fc: Number(f.fc) || 1000,
        gain: Number(f.gain) || 0,
        q: Number(f.q) || 1.414,
        on: f.on !== false,
      })),
    });
    setSelected(null);
    setSaveAsName(row.label);
  }, []);

  // Open on the first entry: the page should never open empty when the app
  // already has profiles installed.
  useEffect(() => {
    if (draft || !all.length) return;
    load(all[0]);
  }, [all, draft, load]);

  const saved = all.find((p) => p.id === draft?.sourceId) ?? null;
  const isPreset = !!saved && presets.some((p) => p.id === saved.id);
  const editable = !!draft && (!draft.sourceId || !isPreset);

  const patch = (partial: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...partial } : d));
  const patchBand = (i: number, p: Partial<EqBand>) =>
    setDraft((d) =>
      d ? { ...d, filters: d.filters.map((f, j) => (j === i ? { ...f, ...p } : f)) } : d);
  const addBand = () => {
    setDraft((d) => {
      if (!d) return d;
      setSelected(d.filters.length);
      return { ...d, filters: [...d.filters, { ...EQ_NEW_BAND }] };
    });
  };
  const removeBand = (i: number) => {
    setDraft((d) => (d ? { ...d, filters: d.filters.filter((_, j) => j !== i) } : d));
    setSelected(null);
  };

  /** Store the draft under a name. One endpoint for both: mlo.eq's import
   *  REPLACES a profile of the same name, which is what saving an edit is. */
  const store = async (name: string, ok: string) => {
    if (!draft) return;
    setBusy(true);
    try {
      const row = await api.exportEqImport(name, eqToApoText(draft.filters, draft.preampDb));
      await qc.invalidateQueries({ queryKey: ["exportEq"] });
      load(row);
      toast.success(ok.replace("{name}", row.label));
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const removeProfile = async (id: string) => {
    try {
      await api.exportEqDelete(id);
      await qc.invalidateQueries({ queryKey: ["exportEq"] });
      if (draft?.sourceId === id) setDraft(null);
      toast.success("Profile deleted");
    } catch (e) {
      toast.error(String(e));
    }
  };

  if (catalog.isLoading) return <PageLoading label="Reading the equalizer profiles…" />;

  return (
    <div className="p-4 md:p-6 space-y-4">
      <PageHeader
        title="Equalizer"
        subtitle="Equalizer APO / Peace profiles — presets, AutoEq headphone corrections and your own curves, baked into an export."
      />

      <div className="grid gap-4 lg:grid-cols-[19rem_minmax(0,1fr)]">
        {/* ---- profile rail ------------------------------------------------ */}
        <aside className="space-y-3">
          <section className="rounded-lg border border-border bg-panel/50 p-3 space-y-2">
            <div className="flex items-center justify-between">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">Profiles</div>
              <span className="text-[10px] text-zinc-500">{all.length}</span>
            </div>
            <div className="space-y-1 max-h-[45vh] overflow-y-auto overscroll-contain">
              {all.map((row) => {
                const isPresetRow = presets.some((p) => p.id === row.id);
                const active = draft?.sourceId === row.id;
                return (
                  <div
                    key={row.id}
                    className={`group flex items-center gap-1 rounded-md border px-2 py-1.5 ${
                      active ? "border-accent/60 bg-raise" : "border-transparent hover:bg-raise/60"
                    }`}
                  >
                    <button
                      className="min-w-0 flex-1 text-left"
                      onClick={() => load(row)}
                      title={isPresetRow ? "Built-in preset" : "Stored profile"}
                    >
                      <div className="text-xs text-zinc-200 truncate">{row.label}</div>
                      <div className="text-[10px] text-zinc-500 truncate">
                        {row.filters?.length ?? 0} band(s)
                        {row.empty ? " · empty" : ""}
                        {row.preamp_db ? ` · preamp ${num(row.preamp_db)} dB` : ""}
                        {isPresetRow ? " · preset" : ""}
                      </div>
                    </button>
                    {!isPresetRow && (
                      <ConfirmButton
                        className="p-1 rounded text-zinc-500 hover:text-red-300 hover:bg-raise shrink-0"
                        title="Delete this profile"
                        confirmLabel="Delete?"
                        onConfirm={() => void removeProfile(row.id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </ConfirmButton>
                    )}
                  </div>
                );
              })}
            </div>
            <div className="flex flex-wrap gap-1.5 pt-1">
              <button className="btn-ghost !py-1 text-xs" onClick={() => setImportOpen(true)}>
                <Upload className="h-3.5 w-3.5" /> Import APO
              </button>
              <button className="btn-ghost !py-1 text-xs" onClick={() => setAutoEqOpen(true)}>
                <Headphones className="h-3.5 w-3.5" /> AutoEq
              </button>
            </div>
          </section>
        </aside>

        {/* ---- editor ------------------------------------------------------ */}
        <section className="space-y-3 min-w-0">
          {!draft ? (
            <div className="rounded-lg border border-border bg-panel/50 p-8 text-center text-xs text-zinc-500">
              Pick a profile to edit, or import one.
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <div className="min-w-0">
                  <div className="text-sm text-zinc-200 truncate">
                    {draft.name}
                    {isPreset && <span className="text-[10px] text-zinc-500"> · built-in preset</span>}
                    {!editable && !isPreset && <span className="text-[10px] text-zinc-500"> · unsaved curve</span>}
                  </div>
                  <div className="text-[11px] text-zinc-500">
                    Edit the curve — nothing is stored until you save.
                  </div>
                </div>
                <div className="ml-auto flex flex-wrap items-center gap-1.5">
                  {editable && (
                    <button
                      className="btn-primary !py-1.5 text-xs"
                      disabled={busy}
                      onClick={() => void store(draft.name, "Saved {name}")}
                      title="Store the curve under this profile's name (replacing it)"
                    >
                      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />} Save
                    </button>
                  )}
                  <button
                    className="btn-ghost !py-1.5 text-xs"
                    onClick={() => {
                      const row = all.find((p) => p.id === draft.sourceId);
                      if (row) load(row);
                      else setDraft({ sourceId: "", name: "New curve", preampDb: 0, filters: [] });
                    }}
                    title="Throw the edits away and reload the stored profile"
                  >
                    <RotateCcw className="h-3.5 w-3.5" /> Revert
                  </button>
                </div>
              </div>

              <EqCurve
                filters={draft.filters}
                preampDb={draft.preampDb}
                selected={selected}
                onSelect={setSelected}
                onChange={patchBand}
              />

              <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-panel/50 px-3 py-2">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500">Preamp</span>
                <input
                  type="range"
                  min={-EQ_PREAMP_LIMIT}
                  max={EQ_PREAMP_LIMIT}
                  step={0.1}
                  value={draft.preampDb}
                  onChange={(e) => patch({ preampDb: Number(e.target.value) })}
                  className="w-48 seek-fat"
                  aria-label="Preamp"
                  title="Headroom in front of the bands — what keeps a boosted curve from clipping"
                />
                <NumField
                  value={draft.preampDb}
                  onCommit={(v) => patch({ preampDb: v })}
                  min={-EQ_PREAMP_LIMIT}
                  max={EQ_PREAMP_LIMIT}
                  digits={2}
                  suffix="dB"
                  title="Preamp in dB, typed exactly"
                />
                <button
                  className="btn-ghost !py-1 text-xs ml-auto"
                  onClick={addBand}
                  title="Add a peaking band in the middle of the spectrum"
                >
                  <Plus className="h-3.5 w-3.5" /> Add band
                </button>
              </div>

              {/* The bands, in the profile's own order — the chain is applied in
                  exactly this order, so the list IS the signal path. */}
              <div className="rounded-lg border border-border overflow-hidden">
                <table className="w-full text-xs">
                  <thead className="bg-raise/60 text-[10px] uppercase tracking-wider text-zinc-500">
                    <tr>
                      <th className="px-2 py-1.5 text-left font-medium w-8">On</th>
                      <th className="px-2 py-1.5 text-left font-medium w-24">Type</th>
                      <th className="px-2 py-1.5 text-left font-medium">Frequency</th>
                      <th className="px-2 py-1.5 text-left font-medium">Gain</th>
                      <th className="px-2 py-1.5 text-left font-medium">Q</th>
                      <th className="px-2 py-1.5 w-8" />
                    </tr>
                  </thead>
                  <tbody>
                    {draft.filters.map((band, i) => {
                      const type = eqType(band.type) || "PK";
                      const shapeOnly = SHAPE_ONLY.has(type);
                      return (
                        <tr
                          key={i}
                          className={`border-t border-border/60 ${i === selected ? "bg-raise/50" : ""}`}
                          onClick={() => setSelected(i)}
                        >
                          <td className="px-2 py-1">
                            <input
                              type="checkbox"
                              checked={band.on}
                              onChange={(e) => patchBand(i, { on: e.target.checked })}
                              title={band.on ? "Applied" : "Skipped — the band keeps its place in the list"}
                              aria-label={`Band ${i + 1} enabled`}
                            />
                          </td>
                          <td className="px-2 py-1">
                            <select
                              className="rounded border border-border bg-panel px-1 py-0.5 text-[11px]"
                              value={type}
                              onChange={(e) => patchBand(i, { type: e.target.value })}
                              title="Equalizer APO filter type"
                            >
                              {EQ_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                            </select>
                          </td>
                          <td className="px-2 py-1">
                            <NumField
                              value={band.fc}
                              onCommit={(v) => patchBand(i, { fc: v })}
                              min={20}
                              max={20000}
                              digits={1}
                              suffix="Hz"
                              title="Centre/corner frequency"
                            />
                          </td>
                          <td className="px-2 py-1">
                            <NumField
                              value={band.gain}
                              onCommit={(v) => patchBand(i, { gain: v })}
                              min={-EQ_GAIN_LIMIT}
                              max={EQ_GAIN_LIMIT}
                              digits={2}
                              suffix="dB"
                              disabled={shapeOnly}
                              title={shapeOnly
                                ? "A pass or a notch has no gain — its shape is the whole filter"
                                : "Band gain"}
                            />
                          </td>
                          <td className="px-2 py-1">
                            <NumField
                              value={band.q}
                              onCommit={(v) => patchBand(i, { q: v })}
                              min={0.1}
                              max={30}
                              digits={2}
                              title="Filter width — higher is narrower"
                            />
                          </td>
                          <td className="px-1 py-1">
                            <button
                              className="p-1 rounded text-zinc-500 hover:text-red-300 hover:bg-raise"
                              onClick={(e) => { e.stopPropagation(); removeBand(i); }}
                              title="Remove this band"
                              aria-label={`Remove band ${i + 1}`}
                            >
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                    {!draft.filters.length && (
                      <tr className="border-t border-border/60">
                        <td colSpan={6} className="px-3 py-4 text-center text-[11px] text-zinc-500">
                          No bands — the curve is flat. Add one, or import a profile.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {/* What the source file said that the editor does not carry: the
                  same sentences the import reported, kept in front of the user
                  for as long as the profile is loaded. */}
              {saved && ((saved.errors?.length ?? 0) > 0 || (saved.unsupported?.length ?? 0) > 0
                || (saved.notes?.length ?? 0) > 0) ? (
                <div className="rounded-lg border border-amber-900/40 bg-amber-950/20 px-3 py-2 space-y-1">
                  {saved.errors?.length ? (
                    <p className="text-[11px] text-red-300">
                      {saved.errors.length} unreadable line(s) — this profile cannot be applied:{" "}
                      {saved.errors[0]}
                    </p>
                  ) : null}
                  {saved.unsupported?.length ? (
                    <p className="text-[11px] text-amber-200/90">
                      {saved.unsupported.length} line(s) the app has no equivalent for and does not
                      apply: {saved.unsupported.slice(0, 2).join(" · ")}
                      {saved.unsupported.length > 2 ? ` (+${saved.unsupported.length - 2} more)` : ""}
                    </p>
                  ) : null}
                  {saved.notes?.map((n, i) => (
                    <p key={i} className="text-[11px] text-zinc-400">{n}</p>
                  ))}
                  <p className="text-[11px] text-zinc-500">
                    Saving rewrites this profile as the bands above, so those lines are gone from the
                    stored file — the curve is what the editor shows.
                  </p>
                </div>
              ) : null}

              {/* Save as new: the only way off a built-in preset (a preset id is
                  reserved, so a curve derived from one needs its own name). */}
              <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-panel/50 px-3 py-2">
                <input
                  className="flex-1 min-w-[12rem] rounded-md border border-border bg-panel px-2 py-1.5 text-xs"
                  value={saveAsName}
                  onChange={(e) => setSaveAsName(e.target.value)}
                  placeholder="Profile name"
                  aria-label="Profile name"
                />
                <button
                  className="btn-ghost !py-1.5 text-xs"
                  disabled={busy || !saveAsName.trim()}
                  onClick={() => void store(saveAsName.trim(), "Saved {name}")}
                  title={
                    isPreset || !draft.sourceId
                      ? "Store these bands as a new profile"
                      : "Store these bands under this name (a new profile if the name is new)"
                  }
                >
                  <Download className="h-3.5 w-3.5" /> Save as
                </button>
              </div>
            </>
          )}
        </section>
      </div>

      {importOpen && (
        <EqProfileModal
          onClose={() => setImportOpen(false)}
          onImported={(row) => {
            setImportOpen(false);
            load(row);
          }}
        />
      )}
      {autoEqOpen && (
        <EqAutoEqDialog
          onClose={() => setAutoEqOpen(false)}
          onImported={(row) => {
            void qc.invalidateQueries({ queryKey: ["exportEq"] });
            setAutoEqOpen(false);
            load(row);
          }}
        />
      )}
    </div>
  );
}
