# la musica 3.18.0 — the cover that lands is the cover you picked, and the player's text stops sitting on grey slabs

Everything below came from reports on the running app, and every one is fixed where the
cause was rather than where the symptom showed. Evidence is named per section; the house
rules live in `docs/OPTIMIZATION-GRADING-SPEC.md`.

## The cover that lands is the cover you picked

3.17.0 fixed *whether* a pick reached the folder (the wizard's `staged` allowance was
never sent, so every "Use this cover" was answered 400). This release fixes *which image*
reaches it, which is what was left: a picked cover could still come out as a completely
different album's artwork.

Measured on the owner's own library, on **In Rainbows**: `cover.jpg` in the album folder
was **OK Computer's** Cover Art Archive front cover — pixel distance 0.18 from the OK
Computer folder's own `cover.jpg` and 0.02 from the cached CAA file, while the row the
user had picked was Apple Music's 4000×4000 In Rainbows artwork. Three separate causes
sat behind it.

- **A substitute was cached under the URL it replaced.** The art proxy answers a URL that
  a provider refuses by asking another provider — and stored *those* bytes under the
  requested URL, for 30 days. So the row's URL was poisoned by whichever search fetched
  it first: at 07:19 the OK Computer finder requested the Apple row `…/634904032463.png`
  (COV returns that row for both searches), the CDN refused, and OK Computer's CAA cover
  was cached under it. At 08:37 the In Rainbows cover step asked for the same row's URL,
  got the cached answer, and wrote **another album's cover** into the folder. Every cache
  entry is now keyed — and only read — by the URL that actually answered, and entries
  written in the old format (which could hold a substitute) are not read at all.
- **A cover write may no longer substitute.** `POST /api/cover/fromurl` and the import
  chain's cover step fetch exactly the picked/chosen URL
  (`_cover_url_bytes(..., substitute=False)`): when it cannot be fetched the write fails
  with its own sentence — *"that image could not be fetched — nothing was written in its
  place"* — instead of landing some other picture on the album. The one repair still
  allowed is the same picture: an Apple storefront URL that answers HTTP 200 with an
  EMPTY body is fetched as the same artwork's largest `image/thumb` transform.
- **That Apple repair is what made the row's own image fetchable at all.** COV's Apple
  rows point `big` at `a1.mzstatic.com/r40/…/<id>.png`, and for some assets that copy
  answers **200 with an empty body** (verified live, while the same asset's
  `…/<id>.png/3000x3000bb.jpg` transform answers 1.8 MB). Every such row therefore went
  to the fallback chain on download. The size probe reads the same copy the download will
  get, so the ranking and the file agree.

*Driven live on a scratch server, with the row from the incident:* the write reports
`1200×1200`, and the file is the picked row's own artwork (distance **0.20** to Apple's
3000×3000 transform of it, **138.06** to the OK Computer cover it used to produce); a
picked URL that cannot be fetched answers 400 and writes nothing; the display path still
renders a repaired image for a dead Apple URL.

*If a cover of yours was replaced by another album's art before this release, the folder
still holds it* — nothing here rewrites art that is already on disk. Re-pick it in the
album's **Find cover** window; the row now writes the image it shows.

## The finder's thumbnails are that row's, never this album's

A candidate's thumbnail was asked about through the art proxy with the **open album's**
release group attached, so a row about another release (COV's name search answers with
karaoke and tribute releases too) whose own URL failed was replaced by the open album's
cover — the one thing a candidate's thumbnail must never be. Each row's art request now
carries that row's own artist and title and nothing else, and the write endpoint no
longer accepts identity it would only have used to substitute.

## Genre labels read as sentences

`genre:` is `Genre:` wherever a person reads it — the Discover rows' reasons, the album
card's `Same genre:` badge, and the rest of the recommendation reasons (`Sounds like …`,
`More from …`, `More release groups by …`, `Same family:`, `Same mood:`, `Same artist:`,
`Energy 45 near 60`, `Both 2007`, `Chart #1 …`, `Most listened this month (ListenBrainz)`,
`Similar to …`). A genre NAME inside one of those lines is now rendered in the app's own
display form — the same Title Case the stored tag and the Discover genre list already use
(spec rule R41), so a row reads `Genre: Alternative Rock (MusicBrainz)` and a badge reads
`Same genre: Shoegaze`, never a lowercase name sitting beside a list that says
`Alternative Rock`. A provider's compound label is left as published (`Rap/Hip Hop`), and
what is SENT to a provider as the search seed keeps its own spelling — only what a person
reads changed.

## The fullscreen player paints no panel on the artwork

The fullscreen view's text sat on two flat grey slabs — a 35 % scrim across the lyrics
and another under the title. Over the artwork that reads as boxes pasted on the cover
(issue **#45**, with a screenshot of the offending layout: "I don't want backgrounds on
UI elements. Text should simply always be visible … make it blurred / opaque").

Every floating surface of the player now draws a **veil** instead of a background: the
same polarity tint the ink table already picks, under a backdrop blur, weak enough to
keep the picture visible through it, and dissolved at its edges (`np-veil`,
`np-veil-pane`, `np-veil-pill`, `np-veil-panel` in `web/src/index.css`; the polarity is
stamped once on the fullscreen root and inherits). The lyrics pane's layer is a ramp with
a top/bottom mask rather than a rectangle, so nothing is drawn at its corners or sides;
the metadata block's tint holds under its glyphs and spreads past the box as a blurred
halo; the player's own menus and the queue drawer are frosted instead of opaque
`bg-zinc-950` panels.

Measured, not eyeballed — and the measurements are the repo's own checks, re-run against
a scratch server for this release:

- `tools/check_fullscreen_player.cjs` → **25/25**: nothing is painted over the artwork,
  no border/ring is drawn on it, the pane's toggle, collapse/restore, scroll position and
  active line all survive (at 1568×817, 1440×900 and 1920×1080).
- `tools/check_np_metadata_contrast.cjs` → **16/16**: every metadata tier ≥ 4.5:1 and the
  title ≥ 3:1 against the field it now sits on, on both polarities (mid-grey cover field
  `rgb(49,49,51)`: title 13.02:1; light cover field `rgb(163,163,164)`: title 7.89:1).
- Both lyric surfaces keep the shared size control and the keyboard `:focus-visible`
  rings (spec R56c) untouched.
