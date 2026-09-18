import { useState } from "react";
import { api } from "../api";

/** "Test connection" for the AI setup — one tiny chat round trip that proves
 *  the base URL / key / model actually answer.
 *
 *  Shared by the setup wizard (which tests the values in its own draft before
 *  saving) and Settings → AI (which tests the saved/edited values): the caller
 *  passes a config-shaped record, so both surfaces send exactly what they
 *  would write. A refused provider is shown, not thrown — its message (bad
 *  key, unknown model, wrong URL) is the whole point of the button. */
export default function AiTestButton({ value }: { value: Record<string, unknown> }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const test = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.aiTest({
        base_url: String(value.ai_base_url ?? ""),
        api_key: String(value.ai_api_key ?? ""),
        model: String(value.ai_model ?? ""),
        effort: String(value.ai_effort ?? ""),
      });
      setResult(
        r.ok
          ? { ok: true, text: `Answered: ${(r.reply || "(empty)").slice(0, 120)}` }
          : { ok: false, text: r.error || "the endpoint refused the request" }
      );
    } catch (e) {
      setResult({ ok: false, text: String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-start gap-2 flex-wrap">
      <button
        className="btn-ghost !py-1 text-xs"
        onClick={test}
        disabled={busy || !String(value.ai_model ?? "").trim()}
        title={
          String(value.ai_model ?? "").trim()
            ? "Send one tiny prompt to the endpoint"
            : "Set a model first — a test needs something to ask"
        }
      >
        {busy ? "Testing…" : "Test connection"}
      </button>
      {result && (
        <span className={`text-[11px] break-all ${result.ok ? "text-emerald-300" : "text-red-300"}`}>
          {result.ok ? "✓ " : "✕ "}
          {result.text}
        </span>
      )}
    </div>
  );
}
