# la musica 3.23.2 — the log says why, and the player fits a phone

Everything below came from a running install. Three of the reports were about
rip logs, and the shape of that complaint decided the work: the grading was
right, and nothing in the app said so.

## A rip log that carries a checksum is checked now, and readable

The report: *"17 candidates rejected — log rejected: … score 60 is below the
required 100 … This is just unrealistic, also I don't even know a way to view
logs."*

- **The score was Logchecker's own.** A real EAC log from this library scores
  100; the 60 is Logchecker's arithmetic on a scene rip (`−30` an EAC older than
  0.99, `−10` gap handling). The bar was never wrong, it was just the only thing
  on screen. So the bar stays — `grade_log_score_threshold`, **100 by default**
  — and the reason now carries Logchecker's own note:
  `<file> — Logchecker 60/100, required 100 (…)`.
- **A checksum was not being checked at all.** Logchecker does not compute the
  SHA256 itself — it shells out to the pypi `eac-logchecker` script and prints
  `Checksum: checksum_ok` **either way**. That script was in neither the image
  nor the Dependencies page, so a log with one digit of "Peak level" changed came
  back `Score 100, checksum_ok` and the app recorded it as verified. Measured
  three ways on a real log (untouched / one digit changed / structurally
  edited): without the verifier `ok · unverified · unverified`, with it
  `ok · invalid · invalid`. The verifier is installed now
  (`eac-logchecker==0.8.1` in `server/requirements.txt` and in the Dependencies
  catalogue), the phar's word is only read as evidence when its own helper could
  have checked, and a claimed-but-unchecked checksum reads **`unverified`** —
  never `ok` — through the grader and the audit.
- **The log is readable in the app.** Album details → **Rip log** (or a track's
  readout) opens the log itself: Logchecker's report (score, ripper, version),
  its own `Details:` lines — where a deduction explains itself — this app's
  checksum verdict, and the log's decoded text. `available: false` is said as
  "no scorer installed", never drawn as a score of zero.

## The library says what is failing, at the top

Home and the Library page open with a grading strip: green when every check
passes, and otherwise the specific things that do not — one failing track shown
as **the track**, two or more in one album shown as **the album** — each a link
that jumps to it.

## Cookies: yt-dlp is named, and both accept an extension's export

- The Netscape import already existed for both (one parser, two endpoints);
  what was missing was exposure. **yt-dlp is named in the Keys surface** with
  what it is for and where its cookies go, and the RYM cookie import is
  reachable from the Keys step, not only from the Discovery tab.
- The RYM help text says what is true: a `cookies.txt` in Netscape format (what
  an extension like "Get cookies.txt" produces) is accepted, as is the
  `Cookie:` header from devtools.

## The fullscreen player on a phone

Below `md` the player opens **compact**: one row with the album thumbnail, the
title and the artist · album, the transport and the seek bar. The lyrics button
expands it to the full block and the pane, and collapses it again — one control
for the mode, and it never writes the desktop pane preference. At `md` and up
nothing changes.

## Dependencies install themselves by default

`dependencies_auto_update` is ON: the app's checks are only worth what the tools
behind them are, and a user who never opens the Dependencies page would
otherwise run a library whose log scoring, DR measurement or AccurateRip
evidence silently did nothing. Installs stay inside the dependencies folder —
nothing system-wide — and the pass is capped at one every six hours (untick it
to install nothing without asking).
