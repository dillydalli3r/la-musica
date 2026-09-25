# la musica 3.23.3 — stop it, watch it, and clean up after it

Exporting gained a stop button and a destination that cannot be raced; the app's
progress bars became one per job with a label; and the duplicate
`description (2).txt` files the app was writing into album folders are no longer
made, are reported, and can be removed.

## Exporting what you chose — and stopping it

- **Every file family is its own toggle**, and `.accurip` finally has its own
  switch instead of hiding inside "rip log": eleven families in the file
  selection (audio, cover, lyrics, cue, log, accurip, description, checksum,
  text, playlist, other), rendered from the engine's own table, so the menu, the
  copy pass and the run's report cannot disagree.
- **Cancel.** A long export to a slow drive can be stopped: the run ends at the
  next FILE boundary (never mid-file), everything already written STAYS — it is
  a copy service, and deleting finished files because you stopped the run would
  destroy what you may still want — and the finishing passes (manifest,
  playlist, album ReplayGain, prune) are skipped, because they describe a
  complete export. One press between two exports cannot arm the next one.
- **Two exports can no longer fight over one destination.** The export holds the
  drive it writes into, exactly as it already held the tracks it reads: the
  second one answers 409 instead of copying over the first while its prune
  deletes the other's audio. Exporting album A while album B imports is still
  fine — the paths are disjoint.

## One bar per job, labelled, until it is done

The header's single progress bar is now a stack: a run, an export and an import
can each have their own row, each with its **label** beside the bar ("Export",
"Grade + 4 more", …). A row ends on ONE fact — its producer's own
`progress_end` — instead of the old 2.5-second timer, so a bar no longer blinks
off between steps while the job is running. The export row carries a **Cancel**
button, which is the button above.

## Duplicate sidecars: not created, reported, removable

The app itself was writing `description (2).txt`: the import chain wrote the
description into its staging folder while "Add to library" had already written
it into the album folder, and the organizer's leftover sweep carried the second
copy in under a ` (2)` name. Three of the owner's six albums had one, and the
layout report said `issues: []`.

- The sweep now DROPS a source file that is byte-identical to the one already at
  the destination (a genuinely different file still takes a ` (2)` name rather
  than overwriting anything).
- The layout scan reports a numbered copy sitting beside its canonical name as
  its own finding, with a Trash fix — the canonical file is never the one moved
  — and the Optimization panel draws it.
- So the next scan of a library with duplicates offers them for removal, and no
  new ones are made.

## The grading strip folds itself away

Home and the Library open with the grading verdict and, when the list of findings
is long, the first three with **Read more — N findings** (and **Show less**).
The summary line is always the answer; the list is what you open when you want
it.

## Two audits — one found nothing, one found a real gap

**ReplayGain** was audited end to end and is working where it claims: script 7
measures with the bundled rsgain and writes the four tags; every web-bundle
client (web, desktop, Tauri mobile) applies gain through the SAME WebAudio gain
node, with the equalizer spliced between it and the analyser; the separate
Flutter client applies the same `/api/replaygain` value as its player volume;
and an export's `tags` mode writes the tags after the tag-cleaning pass (so they
survive) while `apply` bakes the gain into the samples and writes none. A
partial album export skips the album gain rather than inventing one from a
subset. Nothing to fix.

**The equalizer's import forms** were audited the same way: one parser turns
every form into one model, and GraphicEQ arrays, Peace `FilterCurve` ladders and
AutoEq results already worked on BOTH paths. What did not were APO's own alias
spellings — `PEQ`, `Modal`, `LPQ`, `HPQ`, `LS 6dB` / `LS 12dB`, `HS 6dB` /
`HS 12dB`, `LSC`/`HSC`, `BW Oct n` — which were refused outright, so a config
straight out of Equalizer APO could not be imported at all. Each alias now maps
to the filter it really is (`LPQ` stays a low-pass, not a peak), `BW Oct n`
becomes the equivalent Q by APO's own relation, and what cannot be carried over
(a shelf's slope, a `Modal` T60) is named in the profile's notes instead of
being silently dropped. `AP` and `IIR` bands, and any band inside an `If:` /
`ElseIf:` block, are reported by name and NOT applied — this app has no
phase-only filter, no coefficient engine and no APO expression evaluator, and
guessing would change the sound. One profile now also gets ONE verdict: a file
that parsed with an error is refused by the import, by the export and by the
player with the same sentence, and both renderers clamp to the same bounds
(fc 20 Hz–20 kHz, gain ±20 dB, Q 0.1–30, preamp ±24 dB), so what you hear is
what an export writes.

See R72 (accepted syntax and the alias table) and R219 (the shared bounds and
the single verdict) in `docs/OPTIMIZATION-GRADING-SPEC.md`.
