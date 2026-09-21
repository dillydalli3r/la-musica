/** The facet rail: pick a facet field the catalogue marks facetable, read its
 *  values with their counts, and click them into the query.
 *
 *  Multi-select is expressed as ONE condition with a LIST value — the engine
 *  reads `eq` with a list as "is any of", so a set of values from one field is
 *  a single OR group. That matters because the spec's `any` is a GLOBAL or:
 *  three separate eq conditions would be OR-ed against every other row too,
 *  while one list-valued condition keeps `match` meaning what it says for the
 *  rest of the query. Applying a selection therefore REPLACES that field's
 *  list condition rather than adding another one. */

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Filter, Search, X } from "lucide-react";
import { api } from "../api";
import type { LibraryCondition, LibraryField } from "../api";
import { NO_VALUE_OPS, useDebounced, useLibraryFields } from "./QueryBuilder";

/** Values shown at once; `total_values` still reports the whole field. */
const VALUE_LIMIT = 100;

/** The op that means "has no value" for a field, in the order the catalogue
 *  spells it: rating is `is_unrated`, everything else is `missing`/`is_empty`.
 *  A facet never lists a blank value, so this is the only way the rail can
 *  offer "Unrated" — and a field whose ops cannot express it simply has no
 *  such row. */
const NO_VALUE_OP_ORDER = ["is_unrated", "missing", "is_empty"];

/** A facet value as a reader should see it: the engine answers numeric facets
 *  with numbers, and a JSON float would print a year as "1993.0". */
function displayValue(v: string | number): string {
  const s = String(v);
  return typeof v === "number" && Number.isInteger(v) ? s.replace(/\.0$/, "") : s || "(empty)";
}

/** What "no value" reads as for this field: an unrated track is unrated, not
 *  merely valueless. */
function missingLabel(def: LibraryField | undefined): string {
  if (def?.field === "rating") return "Unrated";
  return "No value";
}

function noValueOpOf(def: LibraryField | undefined): string | null {
  if (!def) return null;
  for (const op of NO_VALUE_OP_ORDER) if (def.ops.some((o) => o.op === op)) return op;
  return null;
}

/** The values this field already contributes to the query, as the list the
 *  rail's checkboxes reflect. A condition holding a scalar counts as a
 *  one-value selection — a rule written in the smart-playlist editor shows up
 *  here checked instead of looking empty. */
function appliedValues(applied: LibraryCondition[], field: string): (string | number)[] {
  const out: (string | number)[] = [];
  for (const c of applied) {
    if (c.field !== field || c.op !== "eq") continue;
    if (Array.isArray(c.value)) out.push(...(c.value as (string | number)[]));
    else if (c.value !== undefined && c.value !== null && c.value !== "") out.push(c.value as string | number);
  }
  return out;
}

export default function FacetRail({
  applied,
  target,
  onApply,
  className,
}: {
  /** The conditions currently in the query — what the checkboxes reflect. */
  applied: LibraryCondition[];
  /** Whether the counts are of tracks or of albums — the same rows the sheet
   *  below is showing. */
  target: "tracks" | "albums";
  /** Replace everything the rail contributes for this field: `values` as ONE
   *  `eq` list condition (the OR group) and, when `noValue` is set, the
   *  field's own "has no value" condition. A null/empty pair drops both. */
  onApply: (field: string, values: (string | number)[] | null, noValue?: string | null) => void;
  className?: string;
}) {
  const { data: catalogue } = useLibraryFields();
  const facets = useMemo(() => {
    const out: { group: string; fields: LibraryField[] }[] = [];
    for (const g of catalogue?.groups ?? []) {
      const fields = g.fields.filter((f) => f.facetable);
      if (fields.length) out.push({ group: g.label, fields });
    }
    return out;
  }, [catalogue]);
  const [field, setField] = useState("");
  // The first facetable field the catalogue lists, once it has loaded.
  useEffect(() => {
    if (!field && facets.length) setField(facets[0].fields[0].field);
  }, [facets, field]);

  const [q, setQ] = useState("");
  const search = useDebounced(q, 250);
  const { data: facet, isFetching, error } = useQuery({
    queryKey: ["libraryFacets", field, VALUE_LIMIT, search, target],
    queryFn: () => api.libraryFacets(field, VALUE_LIMIT, search, target),
    enabled: !!field,
    staleTime: 60_000,
  });

  const def = useMemo(() => facets.flatMap((g) => g.fields).find((f) => f.field === field), [facets, field]);
  const missingOp = noValueOpOf(def);
  const appliedHere = useMemo(() => appliedValues(applied, field), [applied, field]);
  const appliedMissing = applied.some((c) => c.field === field && NO_VALUE_OPS[c.op]);
  const [picked, setPicked] = useState<(string | number)[]>(appliedHere);
  const [missingOn, setMissingOn] = useState(appliedMissing);
  // A change from the other side (the builder removed the row, another facet
  // click landed) resets the boxes to what the query actually holds.
  const appliedKey = appliedHere.map(String).join("\u0000");
  useEffect(() => { // eslint-disable-line react-hooks/exhaustive-deps
    setPicked(appliedValues(applied, field));
    setMissingOn(appliedMissing);
  }, [appliedKey, appliedMissing, field]);

  const isPicked = (v: string | number) => picked.some((p) => String(p) === String(v));
  const toggle = (v: string | number) =>
    setPicked((cur) => (cur.some((p) => String(p) === String(v)) ? cur.filter((p) => String(p) !== String(v)) : [...cur, v]));
  const dirty = picked.map(String).join("\u0000") !== appliedKey || missingOn !== appliedMissing;

  const values = facet?.values ?? [];
  const total = facet?.total_values ?? 0;

  return (
    <aside className={`rounded-xl border border-border bg-panel/60 p-3 space-y-2.5 ${className ?? ""}`}>
      <div className="flex items-center gap-2">
        <Filter className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
        <select
          className="input !py-1 text-xs"
          value={field}
          onChange={(e) => {
            setField(e.target.value);
            setQ("");
          }}
          aria-label="Facet field"
        >
          {!facets.length && <option value="">No facetable fields</option>}
          {facets.map((g) => (
            <optgroup key={g.group} label={g.group}>
              {g.fields.map((f) => (
                <option key={f.field} value={f.field}>
                  {f.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>

      <div className="relative">
        <Search className="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-600" />
        <input
          className="input !py-1 text-xs pl-8"
          placeholder="Search values…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {!!q && (
          <button
            className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-white"
            title="Clear the search"
            aria-label="Clear the search"
            onClick={() => setQ("")}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <div className="max-h-[340px] overflow-y-auto -mx-1 px-1 space-y-0.5" role="group" aria-label="Facet values">
        {error && <div className="text-xs text-red-400 px-1 py-2">{error instanceof Error ? error.message : String(error)}</div>}
        {!error && !values.length && (
          <div className="text-xs text-zinc-500 px-1 py-2">{isFetching ? "Counting…" : "No values match."}</div>
        )}
        {!!facet?.missing && !!missingOp && (
          <button
            className={`w-full flex items-center gap-2 rounded-lg px-2 py-1 text-left text-xs transition-colors tap ${
              missingOn ? "bg-accent/20 text-white" : "text-zinc-300 hover:bg-white/10"
            }`}
            onClick={() => setMissingOn(!missingOn)}
            aria-pressed={missingOn}
            title={`Rows with no value for this field — matched with "${missingOp}"`}
          >
            <span className={`h-3.5 w-3.5 rounded border shrink-0 inline-flex items-center justify-center ${missingOn ? "bg-accent border-accent on-accent" : "border-border"}`}>
              {missingOn && <Check className="h-2.5 w-2.5" />}
            </span>
            <span className="flex-1 truncate italic text-zinc-400">{missingLabel(def)}</span>
            <span className="text-zinc-500 tabular-nums shrink-0">{facet.missing.toLocaleString()}</span>
          </button>
        )}
        {values.map((v) => {
          const on = isPicked(v.value);
          return (
            <button
              key={String(v.value)}
              className={`w-full flex items-center gap-2 rounded-lg px-2 py-1 text-left text-xs transition-colors tap ${
                on ? "bg-accent/20 text-white" : "text-zinc-300 hover:bg-white/10"
              }`}
              onClick={() => toggle(v.value)}
              aria-pressed={on}
              title={on ? `Selected — click to remove "${displayValue(v.value)}"` : `Select "${displayValue(v.value)}"`}
            >
              <span className={`h-3.5 w-3.5 rounded border shrink-0 inline-flex items-center justify-center ${on ? "bg-accent border-accent on-accent" : "border-border"}`}>
                {on && <Check className="h-2.5 w-2.5" />}
              </span>
              <span className="flex-1 truncate">{displayValue(v.value)}</span>
              <span className="text-zinc-500 tabular-nums shrink-0">{v.count.toLocaleString()}</span>
            </button>
          );
        })}
      </div>

      <div className="flex items-center justify-between gap-2 text-[11px] text-zinc-500">
        <span>
          {total.toLocaleString()} value{total === 1 ? "" : "s"}
          {!!facet && total > values.length && ` · top ${values.length}`}
        </span>
        <span title={`How many ${target} carry each value, across the whole library`}>whole library</span>
      </div>

      <div className="flex items-center gap-2">
        <button
          className="btn-primary !py-1 text-xs flex-1"
          disabled={!field || !dirty || (!picked.length && !missingOn && !appliedHere.length && !appliedMissing)}
          onClick={() => onApply(field, picked.length ? picked : null, missingOn && missingOp ? missingOp : null)}
        >
          {picked.length || missingOn
            ? `Filter by ${picked.length ? `${picked.length} value${picked.length === 1 ? "" : "s"}` : ""}${
                picked.length && missingOn ? " + " : ""
              }${missingOn ? "no value" : ""}`
            : "Clear this field"}
        </button>
        {(!!appliedHere.length || appliedMissing) && (
          <button className="btn-ghost !py-1 !px-2 text-xs" title="Drop this field's conditions" onClick={() => onApply(field, null, null)}>
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </aside>
  );
}
