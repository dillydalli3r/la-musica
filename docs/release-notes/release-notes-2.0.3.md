# la musica 2.0.3 — the fullscreen viewer's art fits its column

The owner's report (2026-10-17): *"fix formatting in the fullscreen viewer, the
player elements, mainly the album cover can get cut off by the lyrics."* Two
measurements of one class of bug — a box sized by a hard number inside a box
sized by a percentage, and a row whose children's widths plus their own gap
added up past it.

## The fullscreen viewer

- **The art is sized by the column it stands in, not by a fixed 448 px.** At the
  owner's 1061×1024 the art's box was 448 square inside the 418 px column the
  lyrics pane leaves; that column is a scroll container, so the art was CLIPPED
  by it — 15 px off each side, which is the cut-off in the report. It is now
  `lg:w-full` with `aspect-square` and the same `max-w-[min(28rem,48vh)]`
  ceiling: 419 px square with the pane up at that window, the full 448 px on a
  wide one, and nothing to clip. The halo's own wrapper stretches with it, so
  the glow still hugs the art.
- **A short window no longer cuts the top of the art.** The left column centred
  its contents with `justify-center`, which in a scroll container pushes the
  first item above the scrollport, out of reach of any scroll position
  (measured at 1440×620: the art's top 21 px cut by the column *and* by the
  body's `lg:overflow-clip`). It centres with auto margins on its first and last
  child instead — the same centring while there is room, zero when there is not,
  so the column scrolls from its own top.
- **The two columns now fit the row.** At `lg` the cover column is 42 % and the
  lyrics pane was 56 %, so with the body's 32 px gap the row was 98 % + 32 px —
  past the body at every window under ~1600 px wide, and the body's
  `lg:overflow-clip` cut the pane's own right edge with it (measured 12 px at
  1061×1024, i.e. part of its right padding and of a wrapped line's last
  glyphs). The pane is 54 %, so 42 + 54 + 32 px stays inside the body at every
  `lg` width.
- The phone and `md` layouts are untouched: below `lg` the art keeps its fixed
  `w-32`/`w-72` boxes and the pane still stacks under the block.

## The contract

`docs/OPTIMIZATION-GRADING-SPEC.md` gains **R439** (§7.83) — the art is never
wider than the column it stands in, the left column safe-centres, and the two
columns of the wide layout never add up past the row — with the measured numbers
and the report that produced them.

## Verification

- `node tools/check_fullscreen_player.cjs` — **133/133** against a scratch
  library built to the check's own fixture (one two-second track with timed
  lyrics, one track with none). Three new measurements: every ancestor of the
  art whose overflow is not `visible` must contain its box, and the box must be
  square (checked with the pane up at 1568×817, and at 1440×900, 1920×1080,
  **1061×1024** — the owner's own window, where the old tree cut 15 px off each
  side of the art — and **1440×620**, where it cut the art's top by 21 px); the
  pane's right edge must be inside the body at those sizes; the art must still
  end up centred in the reading width when the pane is away. On the unfixed tree
  the same check reports 131/133, failing exactly those two sizes
  (`cut {left: 15, right: 15}` and `cut {top: 21}`).
- The other player-surface checks against the same scratch server —
  `check_np_metadata_contrast.cjs` (271/271), `check_viz_corners.cjs` (10/10),
  `check_lyrscroll.cjs` (18/18) — and the web gate (`tsc -b` clean,
  `oxlint src` warnings pre-existing, `npm run build` ok).
  (`check_lyric_press.cjs` cannot run against this scratch fixture: it needs a
  synced lyric long enough to scroll, and its album page never rendered the
  track table here. It is not part of the release gate, and the line-press /
  follow behaviour it covers is measured by `check_lyrscroll.cjs` above.)
- Every Python suite in `tools/` and every Node suite, and
  `python tools/check_versions.py v2.0.3` reporting all 12 copies agreeing.
