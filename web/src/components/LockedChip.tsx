import { Lock } from "lucide-react";
import { useLockKind, useLockWhy } from "../lib/locks";

/** A row's lock mark: the chip this app already uses for a job's kind (see
 *  MAINTAIN → In progress), next to a file a job is working on, so the state
 *  is visible BEFORE the click rather than as a failed play.
 *
 *  Renders nothing for a playable path, so any row can mount it unconditionally.
 *  The tooltip is the SERVER's own refusal sentence — the same words the stream
 *  route answers with — so the row and the 409 can never tell different
 *  stories. */
export default function LockedChip({
  path,
  className = "",
}: {
  path?: string | null;
  className?: string;
}) {
  const kind = useLockKind(path);
  const why = useLockWhy(path);
  if (!kind) return null;
  return (
    <span
      className={`chip text-[10px] border bg-amber-950/30 border-amber-900/60 text-amber-300/90 shrink-0 ${className}`}
      title={why}
      aria-label={why}
    >
      <Lock className="h-2.5 w-2.5 shrink-0" />
      {kind}
    </span>
  );
}
