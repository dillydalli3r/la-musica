/** Grading + audit verdicts (PASS/FAIL) for albums and tracks.
 *
 * Imported by badges, cards, and table rows (see Badges, AlbumCard,
 * LibraryPage, AlbumPage, ArtistPage). */

/** PASS/FAIL verdict styling for a graded + audited album or track. */
export interface TrackStatus {
  key: "pass" | "fail";
  label: string;
  edge: string; // status edge strip / dot color
  tint: string; // row/card background tint
  text: string; // sliver text color
}

/**
 * One condensed verdict for grading + auditing: an album/track is either
 * PASS (graded clean and the audit is REAL or pending) or FAIL (grading
 * found problems, or the audit came back FAKE / MIX). Everything that went
 * into the verdict is shown as details next to the badge.
 */
export function auditFails(audit: string | null | undefined): boolean {
  const a = (audit ?? "").trim().toUpperCase();
  return a === "FAKE" || a === "MIX";
}

/** Verdict for a grading pass flag plus its audit result (FAKE/MIX fails). */
export function statusFor(pass: boolean, audit: string | null | undefined): TrackStatus {
  // Grading marks stay deliberately quiet: a passed album is barely tinted,
  // failures get a muted red — details live in hover titles, not loud chips.
  if (!pass)
    return { key: "fail", label: "FAIL — grading found problems", edge: "bg-red-500/70", tint: "bg-red-950/20", text: "text-red-400/80" };
  if (auditFails(audit)) {
    const a = (audit ?? "").trim().toUpperCase();
    return { key: "fail", label: `FAIL — audit ${a}`, edge: "bg-red-500/70", tint: "bg-red-950/20", text: "text-red-400/80" };
  }
  return { key: "pass", label: "PASS", edge: "bg-emerald-600/50", tint: "", text: "text-emerald-400/60" };
}

/** Tiny mono grade sliver: just "PASS" or "FAIL" (audit detail on hover). */
export function gradeSliver(pass: boolean, audit: string | null | undefined): string {
  return statusFor(pass, audit).key.toUpperCase();
}
