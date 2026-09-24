/** The check-stack page's gate model — ONE rule, in ONE place.
 *
 *  A script whose feature is switched off is skipped by every run, and a script
 *  with TWO switches runs while ANY of them is on: 17 transliterates, translates
 *  or both (`server/script_runners._DISABLED`), and `run_script` skips only
 *  `if not any(...)`. `server/api_stack.py` states that aggregate per script as
 *  `gate.enabled`, and the page READS it instead of spelling the rule again —
 *  the page had `.every(...)` in three places, which showed a two-switch script
 *  as "gated off" while the run would have run it.
 *
 *  Consequences the page keeps:
 *    * the draft holds ONE boolean per SCRIPT — what the payload states and what
 *      `PUT /api/stack` writes (its `enabled` / `gate_enabled` sets every one of
 *      that script's switches), so the two switches of one script can never
 *      drift apart in the page and be saved half-applied;
 *    * `gateOn` answers a row's state from the draft, so an unsaved tick shows
 *      before it is saved;
 *    * `toggleGate` moves the pressed script's state and nothing else.
 *
 *  Kept out of the page so it can be exercised without a browser
 *  (`tools/check_stack_gates.mjs`). */

/** What a script row has to carry for the gate rule to answer. */
export interface GateScript {
  id: number;
  gate: { keys: string[]; enabled: boolean };
}

/** Script id -> is its feature on. */
export type GateState = Record<number, boolean>;

/** The state the server already stated, keyed by script: the payload's own
 *  aggregate, never a re-derivation of it. A script with no feature switch has
 *  nothing to hold off, so it reads as on (its slot in the chain decides). */
export function gateStateFrom(scripts: readonly GateScript[]): GateState {
  const out: GateState = {};
  for (const s of scripts) out[s.id] = s.gate.keys.length ? s.gate.enabled : true;
  return out;
}

/** Whether the chain would run *s* as the draft stands. */
export function gateOn(gates: GateState, s: GateScript): boolean {
  if (!s.gate.keys.length) return true;
  // A script the draft has never seen (a payload that arrived after the draft
  // was built) falls back to what the server stated.
  return gates[s.id] ?? s.gate.enabled;
}

/** Set *s*'s feature on or off — the whole script, which is the granularity the
 *  payload and the save both speak; every other script is untouched. */
export function toggleGate(gates: GateState, s: GateScript, on: boolean): GateState {
  if (!s.gate.keys.length) return gates;
  return { ...gates, [s.id]: on };
}
