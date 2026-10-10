/** The Soulseek availability dot that rides the nav icon's corner: green =
 *  logged in, amber = slskd up but not authenticated, hidden = not running.
 *  The `dot` is the shape `useSlskDot` returns, so the poll and the mark stay
 *  one contract. Pulled out of App.tsx so the shell file holds the shell. */
export default function SlskIconDot({
  dot,
}: {
  dot: { cls: string; tip: string; name?: string | null } | null;
}) {
  if (!dot) return null;
  return (
    <span
      className={`absolute -top-1 -right-1.5 h-2 w-2 rounded-full ${dot.cls} ring-2 ring-panel`}
      title={dot.tip}
    />
  );
}