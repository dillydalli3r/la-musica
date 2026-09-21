/** The library query builder: condition rows driven entirely by the server's
 *  field catalogue, an all/any switch, and a live match count.
 *
 *  ONE spec, three surfaces. This builder, the facet rail and the smart
 *  playlist rule editor all produce
 *  `{conditions:[{field,op,value}], match}` — the shape mlo/query.py evaluates
 *  and a saved smart playlist stores — so an ad-hoc query and a saved playlist
 *  can never disagree about what matches. That is why nothing here invents a
 *  private query model: a row IS a condition.
 *
 *  Nothing hardcodes a field, an operator or an option list either: the
 *  catalogue owns all three (`/api/library/fields`), so a field added on the
 *  server appears in every picker at once. A condition naming a field the
 *  catalogue does not list — an older saved smart playlist — still renders,
 *  against a fallback op list, and is NEVER dropped: editing someone's saved
 *  rule must not quietly delete it. */

import { useEffect, useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronDown, Copy, Plus, Search, Trash2, X } from "lucide-react";
import { api } from "../api";
import type {
  LibraryCondition,
  LibraryField,
  LibraryFieldOp,
  LibraryFields,
} from "../api";
import Popover, { MenuItem } from "./Popover";
import Segmented from "./Segmented";

/** The field catalogue, cached for the session: it only changes when the
 *  server's own field list does. Shared by the builder, the rail and the
 *  playlist rule editor, so one request serves the whole app. */
export function useLibraryFields() {
  return useQuery({
    queryKey: ["libraryFields"],
    queryFn: api.libraryFields,
    staleTime: 5 * 60_000,
  });
}

/** Field key → definition, built once per catalogue. */
export function fieldIndex(catalogue: LibraryFields | undefined): Map<string, LibraryField> {
  const map = new Map<string, LibraryField>();
  for (const g of catalogue?.groups ?? []) for (const f of g.fields) map.set(f.field, f);
  return map;
}

/** The ops the rule editor offered before the catalogue existed. An old saved
 *  spec may name an operator the catalogue no longer lists; the row still
 *  renders (and still evaluates server-side) instead of losing the rule. */
const FALLBACK_OPS: LibraryFieldOp[] = [
  { op: "eq", label: "is" },
  { op: "ne", label: "is not" },
  { op: "contains", label: "contains" },
  { op: "lt", label: "is less than" },
  { op: "lte", label: "is at most" },
  { op: "gt", label: "is greater than" },
  { op: "gte", label: "is at least" },
  { op: "missing", label: "is missing" },
  { op: "present", label: "is present" },
];

/** Ops that take no value: the value control disappears for exactly these, and
 *  a value left over from the previous op is kept rather than thrown away. */
export const NO_VALUE_OPS: Record<string, true> = {
  missing: true,
  present: true,
  is_empty: true,
  is_not_empty: true,
  is_unrated: true,
  is_rated: true,
};

export function opsFor(def: LibraryField | undefined): LibraryFieldOp[] {
  return def?.ops?.length ? def.ops : FALLBACK_OPS;
}


/** The default op for a freshly added field: the catalogue's first — the
 *  engine orders them most-useful-first. */
function defaultOp(def: LibraryField): string {
  return opsFor(def)[0]?.op ?? "eq";
}

/** A brand-new condition for a field the user just picked. Enum/bool fields
 *  start on their first value (a select with nothing chosen would be a row the
 *  user cannot run); everything else starts empty and is marked pending. */
export function newCondition(def: LibraryField): LibraryCondition {
  const op = defaultOp(def);
  const first = def.values?.[0];
  const value =
    NO_VALUE_OPS[op] ? undefined
      : op === "between" ? [def.min ?? 0, def.max ?? 0]
        : def.type === "enum" || def.type === "bool" ? first ?? ""
          : "";
  return { field: def.field, op, value };
}

/** True while a row is not runnable: its op takes a value and none is filled
 *  in yet. Such a row stays in the editor but is left out of the query and the
 *  count — a half-typed row must not make the count claim the whole library. */
export function isPending(c: LibraryCondition): boolean {
  if (NO_VALUE_OPS[c.op]) return false;
  if (c.op === "between") {
    const v = Array.isArray(c.value) ? c.value : [];
    return v.length < 2 || v.some((n) => n === "" || n === null || n === undefined);
  }
  if (Array.isArray(c.value)) return c.value.length === 0;
  return c.value === "" || c.value === null || c.value === undefined;
}

/** The conditions a query can actually run: the completed ones, in order. */
export function runnableConditions(conditions: LibraryCondition[]): LibraryCondition[] {
  return conditions.filter((c) => !isPending(c));
}

/** The filter spec a query runs and a smart playlist stores. */
export function filterSpec(conditions: LibraryCondition[], match: "all" | "any") {
  return { conditions: runnableConditions(conditions), match };
}

/** Debounced copy of a value — the count below, and the facet autocomplete,
 *  must not fire once per keystroke. */
export function useDebounced<T>(value: T, ms = 250): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return settled;
}

export interface MatchCount {
  tracks: number | null;
  albums: number | null;
  /** A pending row was left out of the count. */
  partial: boolean;
  busy: boolean;
  error: string | null;
}

/** "How many rows would this match?" — answered by the engine itself, with
 *  `limit: 0`, so the number is the query's own total rather than a second
 *  client-side implementation of the filter that could disagree with it.
 *
 *  Two requests (tracks, albums) because the count line names both; both are
 *  debounced, and the previous answer is kept on screen while the next one is
 *  in flight so the number does not blink while typing. */
export function useMatchCount(conditions: LibraryCondition[], match: "all" | "any"): MatchCount {
  const spec = useMemo(() => JSON.stringify(filterSpec(conditions, match)), [conditions, match]);
  const settled = useDebounced(spec);
  const parsed = useMemo(
    () => JSON.parse(settled) as { conditions: LibraryCondition[]; match: "all" | "any" },
    [settled]
  );
  const tracks = useQuery({
    queryKey: ["libraryQuery", "tracks", "count", settled],
    queryFn: () => api.libraryQuery({ ...parsed, target: "tracks", limit: 0 }),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
  const albums = useQuery({
    queryKey: ["libraryQuery", "albums", "count", settled],
    queryFn: () => api.libraryQuery({ ...parsed, target: "albums", limit: 0 }),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
  const failed = tracks.error ?? albums.error;
  return {
    tracks: tracks.data?.total ?? null,
    albums: albums.data?.total ?? null,
    partial: runnableConditions(conditions).length !== conditions.length,
    busy: tracks.isFetching || albums.isFetching,
    error: failed ? (failed instanceof Error ? failed.message : String(failed)) : null,
  };
}

/** A bool's stored value is "true"/"false"; the select reads yes/no. */
function boolLabels(v: string | number): string {
  const s = String(v);
  return s === "true" ? "yes" : s === "false" ? "no" : s;
}

/** The value control a field's own type asks for: two number boxes for a
 *  range, a number box, a select over the catalogue's values, or — for any
 *  facetable text field — a text box whose browser autocomplete is fed by
 *  `/api/library/facets?field=…&q=…`, so a genre is offered with the spelling
 *  the library actually uses. */
export function ValueControl({
  def,
  op,
  value,
  onChange,
  className,
}: {
  def: LibraryField | undefined;
  op: string;
  value: LibraryCondition["value"];
  onChange: (v: LibraryCondition["value"]) => void;
  className?: string;
}) {
  const id = useId();
  const [focused, setFocused] = useState(false);
  // A facet's selection is a LIST ("is any of"). It shows as its values so the
  // row still says what it matches; typing over it replaces the group with the
  // single value that was typed, which is what the box appearing to be one
  // value promises.
  const text = Array.isArray(value) ? value.join(", ") : String(value ?? "");
  const query = useDebounced(text, 300);
  const facetable = !!def?.facetable && (def?.type ?? "text") === "text";
  const suggestions = useQuery({
    queryKey: ["libraryFacets", def?.field, "suggest", query],
    queryFn: () => api.libraryFacets(def!.field, 30, query),
    enabled: facetable && focused,
    staleTime: 60_000,
  });

  if (NO_VALUE_OPS[op]) return <span className="text-xs text-zinc-600 italic px-1">no value</span>;

  if (op === "between") {
    const pair = Array.isArray(value) ? value : ["", ""];
    const num = (v: unknown) => (v === "" || v === null || v === undefined ? "" : Number(v));
    return (
      <span className={`flex items-center gap-1.5 ${className ?? ""}`}>
        <input
          className="input !py-1 text-xs min-w-0 flex-1"
          type="number"
          inputMode="decimal"
          min={def?.min}
          max={def?.max}
          step={def?.step}
          aria-label="From"
          placeholder="from"
          value={num(pair[0])}
          onChange={(e) => onChange([e.target.value === "" ? "" : Number(e.target.value), num(pair[1])])}
        />
        <span className="text-zinc-600 text-xs shrink-0">to</span>
        <input
          className="input !py-1 text-xs min-w-0 flex-1"
          type="number"
          inputMode="decimal"
          min={def?.min}
          max={def?.max}
          step={def?.step}
          aria-label="To"
          placeholder="to"
          value={num(pair[1])}
          onChange={(e) => onChange([num(pair[0]), e.target.value === "" ? "" : Number(e.target.value)])}
        />
      </span>
    );
  }

  const list = def?.values ?? [];
  if (def && (def.type === "enum" || def.type === "bool") && list.length) {
    return (
      <select
        className={`input !py-1 text-xs ${className ?? ""}`}
        aria-label="Value"
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">—</option>
        {list.map((v) => (
          <option key={String(v)} value={String(v)}>
            {def.type === "bool" ? boolLabels(v) : String(v)}
          </option>
        ))}
      </select>
    );
  }

  if (def?.type === "number") {
    return (
      <input
        className={`input !py-1 text-xs min-w-0 ${className ?? ""}`}
        type="number"
        inputMode="decimal"
        min={def.min}
        max={def.max}
        step={def.step}
        aria-label="Value"
        placeholder={def.unit ? def.unit : "value"}
        value={text}
        onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
      />
    );
  }

  return (
    <>
      <input
        className={`input !py-1 text-xs min-w-0 ${className ?? ""}`}
        list={facetable ? id : undefined}
        aria-label="Value"
        placeholder={def ? "value" : "type a value"}
        value={text}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onChange={(e) => onChange(e.target.value)}
      />
      {facetable && (
        <datalist id={id}>
          {(suggestions.data?.values ?? []).map((v) => (
            <option key={String(v.value)} value={String(v.value)} />
          ))}
        </datalist>
      )}
    </>
  );
}

/** The field picker: every field the catalogue lists, under its group, with a
 *  search box — the catalogue runs to a few hundred entries once tag families
 *  are in it, so scrolling a flat list would be the whole interaction. */
function FieldPicker({
  field,
  catalogue,
  target,
  onPick,
  className,
}: {
  field: string;
  catalogue: LibraryFields | undefined;
  /** Which result the query is about — a field that is tracks-only says so
   *  while the page is showing albums instead of silently matching nothing. */
  target?: "tracks" | "albums";
  onPick: (def: LibraryField) => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const def = fieldIndex(catalogue).get(field);
  const needle = q.trim().toLowerCase();
  const groups = useMemo(
    () =>
      (catalogue?.groups ?? [])
        .map((g) => ({
          ...g,
          fields: needle
            ? g.fields.filter(
                (f) => f.label.toLowerCase().includes(needle) || f.field.toLowerCase().includes(needle)
              )
            : g.fields,
        }))
        .filter((g) => g.fields.length),
    [catalogue, needle]
  );

  return (
    <div className={`relative shrink-0 ${className ?? ""}`}>
      <button
        className="input !py-1 text-xs text-left flex items-center gap-1.5 justify-between"
        onClick={() => setOpen(!open)}
        title={def ? `${def.label} — ${def.field}` : `${field} (unknown to the field catalogue)`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className={`truncate ${def ? "" : "text-amber-300/80"}`}>{def?.label ?? field}</span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-zinc-500" />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} align="left" panelClass="w-72 max-h-[60vh] overflow-y-auto p-1.5">
        <div className="sticky top-0 -mt-1.5 pt-1.5 pb-1 bg-zinc-950 z-10">
          <div className="relative">
            <Search className="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-600" />
            <input
              className="input !py-1 text-xs pl-8"
              placeholder="Find a field…"
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        </div>
        {!catalogue && <div className="text-xs text-zinc-500 px-2.5 py-2">Loading fields…</div>}
        {catalogue && !groups.length && <div className="text-xs text-zinc-500 px-2.5 py-2">No field matches.</div>}
        {groups.map((g) => (
          <div key={g.id}>
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2.5 pt-2 pb-0.5">{g.label}</div>
            {g.fields.map((f) => (
              <MenuItem
                key={f.field}
                label={fieldNote(f, target) ? `${f.label} — ${fieldNote(f, target)}` : f.label}
                title={[f.field, f.hint, fieldNote(f, target)].filter(Boolean).join(" — ")}
                active={f.field === field}
                onClick={() => {
                  setOpen(false);
                  setQ("");
                  onPick(f);
                }}
              />
            ))}
          </div>
        ))}
      </Popover>
    </div>
  );
}

/** Why a field is worth a warning in the picker, or "" when it is not: a tag
 *  the library payload does not carry yet can only ever match nothing, and a
 *  field that is meaningful for one result only should say so before the
 *  user builds a rule against the other one. */
function fieldNote(f: LibraryField, target?: "tracks" | "albums"): string {
  if (f.in_payload === false) return "no tag in the payload yet";
  if (target && f.targets?.length && !f.targets.includes(target)) return `${f.targets.join("/")} only`;
  return "";
}

/** One condition row. Its op list, value control and option values all come
 *  from the field's own catalogue entry, so a row can never offer an operator
 *  the engine would refuse. */
function ConditionRow({
  cond,
  catalogue,
  target,
  onChange,
  onDuplicate,
  onRemove,
}: {
  cond: LibraryCondition;
  catalogue: LibraryFields | undefined;
  target: "tracks" | "albums";
  onChange: (next: LibraryCondition) => void;
  onDuplicate: () => void;
  onRemove: () => void;
}) {
  const def = fieldIndex(catalogue).get(cond.field);
  const ops = opsFor(def);
  const pending = isPending(cond);
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <FieldPicker
        field={cond.field}
        catalogue={catalogue}
        target={target}
        className="w-[180px]"
        onPick={(f) => onChange(newCondition(f))}
      />
      <select
        className="input !py-1 text-xs w-[132px] shrink-0"
        aria-label="Operator"
        value={cond.op}
        onChange={(e) => onChange({ ...cond, op: e.target.value })}
      >
        {ops.some((o) => o.op === cond.op) ? null : <option value={cond.op}>{cond.op}</option>}
        {ops.map((o) => (
          <option key={o.op} value={o.op}>
            {o.label}
          </option>
        ))}
      </select>
      <ValueControl
        def={def}
        op={cond.op}
        value={cond.value}
        onChange={(v) => onChange({ ...cond, value: v })}
        className="w-[180px]"
      />
      {pending && (
        <span className="text-[10px] text-amber-300/80 shrink-0" title="Left out of the query and the count until it has a value">
          no value yet
        </span>
      )}
      <span className="flex items-center gap-0.5 ml-auto">
        <button className="btn-ghost !px-1.5" title="Duplicate condition" aria-label="Duplicate condition" onClick={onDuplicate}>
          <Copy className="h-3.5 w-3.5" />
        </button>
        <button className="btn-danger !px-1.5" title="Remove condition" aria-label="Remove condition" onClick={onRemove}>
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </span>
    </div>
  );
}

/** The builder itself. Controlled: the caller owns the conditions (a facet
 *  click, a saved playlist's spec and this editor all write the same array),
 *  which is what keeps one query model across the app. */
export default function QueryBuilder({
  conditions,
  match,
  target,
  onChange,
  hint,
  className,
}: {
  conditions: LibraryCondition[];
  match: "all" | "any";
  /** Which count is the live result set — the other is shown muted. */
  target: "tracks" | "albums";
  onChange: (next: { conditions: LibraryCondition[]; match: "all" | "any" }) => void;
  /** Rendered on the footer line (the Browse page puts nothing here yet). */
  hint?: ReactNode;
  className?: string;
}) {
  const { data: catalogue } = useLibraryFields();
  const count = useMatchCount(conditions, match);
  const set = (patch: Partial<{ conditions: LibraryCondition[]; match: "all" | "any" }>) =>
    onChange({ conditions: patch.conditions ?? conditions, match: patch.match ?? match });
  const mutate = (i: number, next: LibraryCondition) =>
    set({ conditions: conditions.map((c, j) => (j === i ? next : c)) });
  const firstField = catalogue?.groups.flatMap((g) => g.fields)[0];

  const tally = (n: number | null) => (n === null ? "…" : n.toLocaleString());
  const active = "text-zinc-200 font-medium";
  const muted = "text-zinc-500";

  return (
    <div className={`space-y-2 ${className ?? ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Segmented
          value={match}
          onChange={(m) => set({ match: m })}
          options={[
            { id: "all", label: "Match all" },
            { id: "any", label: "Match any" },
          ]}
        />
        <span className="text-xs text-zinc-500">
          {conditions.length === 0
            ? "Every track in the library — add a condition to narrow it."
            : `${conditions.length} condition${conditions.length === 1 ? "" : "s"}`}
        </span>
        {conditions.length > 0 && (
          <button className="btn-ghost !px-2 text-xs ml-auto" onClick={() => set({ conditions: [] })} title="Remove every condition">
            <X className="h-3.5 w-3.5" /> Clear
          </button>
        )}
      </div>

      <div className="space-y-1.5">
        {conditions.map((c, i) => (
          <ConditionRow
            key={i}
            cond={c}
            catalogue={catalogue}
            target={target}
            onChange={(next) => mutate(i, next)}
            onDuplicate={() => set({ conditions: [...conditions.slice(0, i + 1), { ...c }, ...conditions.slice(i + 1)] })}
            onRemove={() => set({ conditions: conditions.filter((_, j) => j !== i) })}
          />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          className="btn-ghost text-xs"
          disabled={!firstField}
          onClick={() => firstField && set({ conditions: [...conditions, newCondition(firstField)] })}
        >
          <Plus className="h-3.5 w-3.5" /> Add condition
        </button>
        <span className="text-xs" aria-live="polite">
          {count.error ? (
            <span className="text-red-400" title={count.error}>
              {count.error}
            </span>
          ) : (
            <>
              <span className="text-zinc-500">matches </span>
              <span className={target === "tracks" ? active : muted}>{tally(count.tracks)} tracks</span>
              <span className="text-zinc-600"> · </span>
              <span className={target === "albums" ? active : muted}>{tally(count.albums)} albums</span>
              {count.partial && <span className="text-zinc-600"> (incomplete rows skipped)</span>}
            </>
          )}
        </span>
        {hint}
      </div>
    </div>
  );
}
