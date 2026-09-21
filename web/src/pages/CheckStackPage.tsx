import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown as Down, ChevronUp as Up, ClipboardCheck, Layers, RefreshCw,
  RotateCcw, Save, ShieldCheck, SunMedium, ToggleRight,
} from "lucide-react";
import { getToken, serverUrl } from "../api";
import { toast } from "../store";
import Segmented from "../components/Segmented";
import { PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";

/** MAINTAIN → Check stack: the whole stack in one page.
 *
 *  Everything rendered here comes from `GET /api/stack` (server/api_stack.py),
 *  which derives it from the code that owns each fact — the runner registry,
 *  the menu table in `mlo.cli`, the tag registry built from DEFAULT_CONFIG, the
 *  grader's own gate set and `mlo.audit`'s tables. This file therefore holds NO
 *  list of scripts, checks, groups or presets: a check added to the grader
 *  appears here the moment it exists, and a group can only change in both
 *  places at once (tools/test_check_stack.py compares them).
 *
 *  Edits are local until Save, then go through `PUT /api/stack`, which writes
 *  the same config keys the scripts and the grader already read. */

interface StackScript {
  id: number;
  label: string;
  description: string;
  in_order: boolean;
  enabled: boolean;
  order: number | null;
  gate: { keys: string[]; enabled: boolean };
  /** False for the ids the server re-anchors into the Run All order on every
   *  load — only a feature switch (gate.keys) can keep one of those out. */
  removable: boolean;
  available: boolean;
}

interface StackCheck {
  key: string;
  label: string;
  default: boolean;
  tags: string[];
  group: string;
  description: string;
  enabled: boolean;
  issue_codes: string[];
  writers: string[];
  presets: string[];
}

interface StackAuditStep {
  key: string;
  label: string;
  kind: "bool" | "number";
  default: boolean | number;
  enabled: boolean | number;
  flag: string | null;
  used: boolean;
  description: string;
}

interface StackPreset { id: string; label: string; description: string; keys: string[] }

interface Stack {
  scripts: StackScript[];
  run_all_order: number[];
  checks: StackCheck[];
  groups: { id: string; title: string; keys: string[] }[];
  presets: StackPreset[];
  audit: { script: { id: number; label: string; description: string }; steps: StackAuditStep[] };
  totals: { scripts: number; scripts_enabled: number; checks: number; checks_enabled: number };
  grader_gates: string[];
}

interface StackEdit {
  order?: number[];
  scripts?: Record<string, { enabled?: boolean }>;
  checks?: Record<string, boolean>;
  audit?: Record<string, boolean>;
  preset?: string;
}

/** The stack endpoint, through `serverUrl()` + the session token rather than a
 *  bare relative path: the Tauri shells serve the UI from tauri://localhost and
 *  talk to a backend on another address (the same reason lib/tags.ts fetches
 *  its registry this way). */
function headers(json: boolean): Record<string, string> {
  const h: Record<string, string> = { Accept: "application/json" };
  if (json) h["Content-Type"] = "application/json";
  const token = getToken();
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

async function fetchStack(): Promise<Stack> {
  const r = await fetch(`${serverUrl()}/api/stack`, {
    credentials: "include",
    headers: headers(false),
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`check stack: HTTP ${r.status}`);
  return (await r.json()) as Stack;
}

async function putStack(edit: StackEdit): Promise<Stack> {
  const r = await fetch(`${serverUrl()}/api/stack`, {
    method: "PUT",
    credentials: "include",
    headers: headers(true),
    body: JSON.stringify(edit),
    signal: AbortSignal.timeout(30000),
  });
  if (!r.ok) {
    // The server names the offending id (400) or the save failure (500); show
    // its own words instead of a generic "HTTP 400".
    let detail = `check stack: HTTP ${r.status}`;
    try {
      const body = (await r.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body: the status line is all there is */
    }
    throw new Error(detail);
  }
  const body = (await r.json()) as { stack: Stack };
  return body.stack;
}

const STACK_KEY = ["stack"] as const;

/** The editable part of the stack: the chain order, each check's state, each
 *  audit step's state and each script's feature switch. */
interface Draft {
  order: number[];
  checks: Record<string, boolean>;
  audit: Record<string, boolean>;
  gates: Record<string, boolean>;
}

function draftFrom(stack: Stack): Draft {
  const gates: Record<string, boolean> = {};
  for (const s of stack.scripts) for (const k of s.gate.keys) gates[k] = s.gate.enabled;
  return {
    order: [...stack.run_all_order],
    checks: Object.fromEntries(stack.checks.map((c) => [c.key, c.enabled])),
    audit: Object.fromEntries(stack.audit.steps.map((s) => [s.key, !!s.enabled])),
    gates,
  };
}

export default function CheckStackPage() {
  const qc = useQueryClient();
  const { data: stack, isError, refetch } = useQuery({
    queryKey: STACK_KEY,
    queryFn: fetchStack,
    staleTime: 30000,
  });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [q, setQ] = useState("");

  useEffect(() => {
    if (stack && draft === null) setDraft(draftFrom(stack));
  }, [stack, draft]);

  const baseline = useMemo(() => (stack ? draftFrom(stack) : null), [stack]);
  const dirty = !!draft && !!baseline
    && JSON.stringify(draft) !== JSON.stringify(baseline);

  const save = async () => {
    if (!draft || !stack || !baseline) return;
    const edit: StackEdit = {};
    if (JSON.stringify(draft.order) !== JSON.stringify(baseline.order)) edit.order = draft.order;
    const checks: Record<string, boolean> = {};
    for (const c of stack.checks) if (draft.checks[c.key] !== c.enabled) checks[c.key] = draft.checks[c.key];
    if (Object.keys(checks).length) edit.checks = checks;
    const audit: Record<string, boolean> = {};
    for (const s of stack.audit.steps) if (s.kind === "bool" && draft.audit[s.key] !== !!s.enabled) audit[s.key] = draft.audit[s.key];
    if (Object.keys(audit).length) edit.audit = audit;
    const scripts: Record<string, { enabled: boolean }> = {};
    for (const s of stack.scripts) {
      const now = s.gate.keys.length ? s.gate.keys.every((k) => draft.gates[k]) : s.in_order;
      if (now !== s.enabled) scripts[String(s.id)] = { enabled: now };
    }
    if (Object.keys(scripts).length) edit.scripts = scripts;

    setSaving(true);
    try {
      const next = await putStack(edit);
      qc.setQueryData(STACK_KEY, next);
      setDraft(draftFrom(next));
      toast.success("Check stack saved");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setSaving(false);
    }
  };

  const discard = () => {
    if (baseline) setDraft(baseline);
    toast("Changes discarded");
  };

  /** A named preset is applied locally and written by Save: its membership is
   *  the server's own answer per check (`presets`), so it can never disagree
   *  with what Settings → Grading's preset does. */
  const [preset, setPreset] = useState<"strict" | "balanced" | "relaxed">("balanced");
  const applyPreset = (pid: "strict" | "balanced" | "relaxed") => {
    if (!stack) return;
    setPreset(pid);
    setDraft((d) => {
      if (!d) return d;
      const checks = { ...d.checks };
      for (const c of stack.checks) checks[c.key] = c.presets.includes(pid);
      return { ...d, checks };
    });
    toast(`${pid[0].toUpperCase() + pid.slice(1)} preset loaded — Save to apply`);
  };

  const moveScript = (id: number, delta: number) =>
    setDraft((d) => {
      if (!d) return d;
      const i = d.order.indexOf(id);
      if (i < 0) return d;
      const j = i + delta;
      if (j < 0 || j >= d.order.length) return d;
      const order = [...d.order];
      order.splice(j, 0, ...order.splice(i, 1));
      return { ...d, order };
    });

  const toggleScript = (s: StackScript, on: boolean) =>
    setDraft((d) => {
      if (!d) return d;
      if (s.gate.keys.length) {
        const gates = { ...d.gates };
        for (const k of s.gate.keys) gates[k] = on;
        const order = on && !d.order.includes(s.id) ? [...d.order, s.id] : d.order;
        return { ...d, gates, order };
      }
      return { ...d, order: on ? [...d.order, s.id] : d.order.filter((i) => i !== s.id) };
    });

  const needle = q.trim().toLowerCase();
  // The filter matches what the row shows — its label and the description,
  // which names the config key — so typing a key or a script id finds it.
  const visible = <T extends { label: string; description: string }>(rows: T[]) =>
    needle
      ? rows.filter((r) => `${r.label} ${r.description}`.toLowerCase().includes(needle))
      : rows;

  const PRESETS: { id: "strict" | "balanced" | "relaxed"; label: string; icon: typeof ShieldCheck }[] = [
    { id: "strict", label: "Strict", icon: ShieldCheck },
    { id: "balanced", label: "Balanced", icon: ToggleRight },
    { id: "relaxed", label: "Relaxed", icon: SunMedium },
  ];

  if (!draft || !stack || !baseline) {
    return (
      <div className="p-6 max-w-6xl mx-auto">
        <PageHeader sticky icon={Layers} title="Checks & scripts" subtitle="Every script, every grading check and the audit pass, in one place." />
        <div className="panel text-sm text-zinc-400 space-y-2">
          {isError ? (
            <>
              <div className="text-amber-300">Could not load the stack — the server may be restarting, or this server build predates the endpoint (server/api_stack.py).</div>
              <button className="btn-ghost !py-1.5 text-xs tap" onClick={() => refetch()}>
                <RefreshCw className="h-3.5 w-3.5" /> Retry
              </button>
            </>
          ) : (
            <PageLoading label="Loading the stack…" />
          )}
        </div>
      </div>
    );
  }

  // The chain first, then every script that is out of it (its row keeps the
  // tick that puts it back at its default position).
  const ordered = [
    ...draft.order.map((id) => stack.scripts.find((s) => s.id === id)).filter((s): s is StackScript => !!s),
    ...stack.scripts.filter((s) => !draft.order.includes(s.id)),
  ];
  const checksOn = stack.checks.filter((c) => draft.checks[c.key]).length;
  const scriptsOn = stack.scripts.filter((s) => draft.order.includes(s.id)
    && (s.gate.keys.length ? s.gate.keys.every((k) => draft.gates[k]) : true)).length;

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        sticky
        icon={Layers}
        title="Checks & scripts"
        subtitle="The whole optimization, grading and auditing stack — the Run All chain, every graded check with its group and preset, and the audit pass. Everything here is described by the server from the code that owns it (server/api_stack.py), and saves to the same settings the scripts and the grader already read."
        chips={[`${scriptsOn}/${stack.totals.scripts} scripts`, `${checksOn}/${stack.totals.checks} checks`]}
        actions={
          <>
            {dirty && <span className="text-[10px] font-mono text-amber-400/80">unsaved changes</span>}
            <button className="btn-ghost !py-1.5 text-xs tap" onClick={discard} disabled={!dirty || saving}>
              <RotateCcw className="h-3.5 w-3.5" /> Discard
            </button>
            <button className="btn-primary !py-1.5 text-xs tap" onClick={save} disabled={!dirty || saving}>
              <Save className="h-3.5 w-3.5" /> {saving ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        <div className="flex items-center gap-2 flex-wrap">
          <input
            className="input !py-1.5 text-xs w-full sm:max-w-xs tap"
            placeholder="Filter scripts and checks…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <Segmented value={preset} onChange={applyPreset} options={PRESETS} />
          <span
            className="chip font-mono bg-white/5 border border-border text-zinc-400"
            title="The server derives these from DEFAULT_CONFIG and the grader's own gates; the test builds the same two sets from the code and fails when they differ"
          >
            {stack.totals.checks} checks · {stack.grader_gates.length} grader gates
          </span>
        </div>
      </PageHeader>

      {/* ── The Run All chain ───────────────────────────────────────────── */}
      <section className="space-y-1.5">
        <div className="px-1 pt-2 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-semibold">Scripts — the Run All chain</div>
            <div className="text-[11px] text-zinc-500">
              Run All and the import chain execute this sequence. A script with a feature switch is skipped while the
              switch is off, even with its slot kept.
            </div>
          </div>
          <span className="chip font-mono shrink-0 mt-0.5 bg-white/5 border border-border text-zinc-400">
            {scriptsOn}/{stack.totals.scripts} on
          </span>
        </div>
        <div className="stagger divide-y divide-border/40 rounded-lg border border-border/60 bg-panel/40">
          {visible(ordered).map((s) => {
            const on = draft.order.includes(s.id)
              && (s.gate.keys.length ? s.gate.keys.every((k) => draft.gates[k]) : true);
            return (
              <div key={s.id} className="flex items-start gap-3 px-3.5 py-2.5">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={on}
                  onChange={(e) => toggleScript(s, e.target.checked)}
                  title={s.gate.keys.length
                    ? `Switched by ${s.gate.keys.join(", ")} — the chain skips this script while it is off`
                    : s.removable
                      ? "Include this script in the Run All chain"
                      : "The app re-anchors this script into the chain on every load — it has no switch to keep it out"}
                />
                <span className="w-5 shrink-0 text-right font-mono text-[10px] text-zinc-500 mt-1">
                  {draft.order.indexOf(s.id) + 1 || "–"}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="text-sm text-zinc-200 block">
                    {s.id} · {s.label}
                    {!on && s.in_order && <span className="ml-2 text-[10px] text-amber-400/80 font-mono">gated off</span>}
                    {!s.in_order && <span className="ml-2 text-[10px] text-amber-400/80 font-mono">out of the chain</span>}
                    {!s.available && <span className="ml-2 text-[10px] text-amber-400/80 font-mono">not installed</span>}
                  </span>
                  <span className="text-[11px] text-zinc-500 block leading-snug">{s.description}</span>
                  <span className="text-[10px] text-zinc-600 block leading-snug font-mono">
                    {s.gate.keys.length
                      ? `switch ${s.gate.keys.join(" · ")} ${s.gate.enabled ? "on" : "off"}`
                      : s.removable ? "no feature switch" : "position fixed by the app"}
                  </span>
                </span>
                <span className="flex items-center gap-0.5 shrink-0 mt-0.5">
                  <button
                    className="btn-ghost !px-1 !py-0.5 tap min-w-11 md:min-w-0"
                    disabled={draft.order.indexOf(s.id) === 0}
                    onClick={() => moveScript(s.id, -1)}
                    title="Run earlier"
                  >
                    <Up className="h-3 w-3" />
                  </button>
                  <button
                    className="btn-ghost !px-1 !py-0.5 tap min-w-11 md:min-w-0"
                    disabled={draft.order.indexOf(s.id) === draft.order.length - 1}
                    onClick={() => moveScript(s.id, 1)}
                    title="Run later"
                  >
                    <Down className="h-3 w-3" />
                  </button>
                </span>
              </div>
            );
          })}
          {stack.scripts.some((s) => !draft.order.includes(s.id)) && (
            <div className="px-3.5 py-2.5 text-[11px] text-zinc-500">
              A script ticked back on rejoins the chain at its default position, so the pipeline order is never left
              with a hole.
            </div>
          )}
        </div>
      </section>

      {/* ── The grading checks ──────────────────────────────────────────── */}
      {stack.groups.map((g) => {
        const rows = visible(stack.checks.filter((c) => c.group === g.id));
        const all = stack.checks.filter((c) => c.group === g.id);
        const on = all.filter((c) => draft.checks[c.key]).length;
        if (q.trim() && rows.length === 0) return null;
        return (
          <section key={g.id} className="space-y-1.5">
            <div className="px-1 pt-2 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold">{g.title}</div>
                <div className="text-[11px] text-zinc-500">
                  {g.id === "categories"
                    ? "Which file types count toward an album's grade."
                    : `Checks that count for or against an album's grade (${all.length}).`}
                </div>
              </div>
              <span
                className={`chip font-mono shrink-0 mt-0.5 border ${
                  all.length > 0 && on === all.length
                    ? "bg-accent/10 border-accent/25 text-accent-soft"
                    : on === 0 ? "bg-white/5 border-border text-zinc-500" : "bg-white/5 border-border text-zinc-400"
                }`}
                title={`${on} of ${all.length} checks enabled in this group`}
              >
                {on}/{all.length}
              </span>
            </div>
            <div className="stagger divide-y divide-border/40 rounded-lg border border-border/60 bg-panel/40">
              {rows.map((c) => (
                <label key={c.key} className="flex items-start gap-3 px-3.5 py-2.5 cursor-pointer select-none hover:bg-raise/40 transition-colors">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={!!draft.checks[c.key]}
                    onChange={(e) => setDraft((d) => (d
                      ? { ...d, checks: { ...d.checks, [c.key]: e.target.checked } }
                      : d))}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="text-sm text-zinc-200 block flex items-center gap-2 flex-wrap">
                      {c.label}
                      <span className="chip font-mono bg-white/5 border border-border text-[9px] text-zinc-500">
                        {c.default ? "on by default" : "off by default"}
                      </span>
                      {c.presets.length > 0 && (
                        <span className="chip font-mono bg-white/5 border border-border text-[9px] text-zinc-500" title="Presets that turn this check on">
                          {c.presets.join(" · ")}
                        </span>
                      )}
                    </span>
                    <span className="text-[11px] text-zinc-500 block leading-snug">{c.description}</span>
                    {c.writers.length > 0 && (
                      <span className="text-[10px] text-zinc-600 block leading-snug font-mono">
                        cleared by {c.writers.join(" · ")}
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>
          </section>
        );
      })}

      {/* ── The audit pass ──────────────────────────────────────────────── */}
      <section className="space-y-1.5">
        <div className="px-1 pt-2 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-semibold">
              <ClipboardCheck className="inline h-3.5 w-3.5 mr-1 text-accent" />
              Auditing — script {stack.audit.script.id}, {stack.audit.script.label}
            </div>
            <div className="text-[11px] text-zinc-500">{stack.audit.script.description}</div>
          </div>
          <span className="chip font-mono shrink-0 mt-0.5 bg-white/5 border border-border text-zinc-400">
            {stack.audit.steps.filter((s) => s.kind === "bool" && draft.audit[s.key]).length}/
            {stack.audit.steps.filter((s) => s.kind === "bool").length} steps on
          </span>
        </div>
        <div className="stagger divide-y divide-border/40 rounded-lg border border-border/60 bg-panel/40">
          {visible(stack.audit.steps).map((s) => (
            <div key={s.key} className="flex items-start gap-3 px-3.5 py-2.5">
              {s.kind === "bool" ? (
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={!!draft.audit[s.key]}
                  onChange={(e) => setDraft((d) => (d
                    ? { ...d, audit: { ...d.audit, [s.key]: e.target.checked } }
                    : d))}
                />
              ) : (
                <span className="w-4 shrink-0" />
              )}
              <span className="min-w-0 flex-1">
                <span className="text-sm text-zinc-200 block">{s.label}</span>
                <span className="text-[11px] text-zinc-500 block leading-snug">{s.description}</span>
              </span>
              {s.kind === "number" && (
                <span className="chip font-mono bg-white/5 border border-border text-[10px] text-zinc-400 shrink-0">
                  {String(s.enabled)}
                </span>
              )}
              {!s.used && (
                <span className="chip font-mono bg-amber-500/10 border border-amber-500/30 text-[10px] text-amber-300 shrink-0">
                  not read
                </span>
              )}
            </div>
          ))}
        </div>
        <div className="px-1 text-[10px] text-zinc-600">
          Numbers are shown as they are set; the toggles here write the same keys Settings → Audit does.
        </div>
      </section>
    </div>
  );
}
