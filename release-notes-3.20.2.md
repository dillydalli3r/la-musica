# la musica 3.20.2 — text you can read on every cover, and an import that gets out of its own way

Two reports from the running app: the fullscreen player's text vanishing into
the artwork, and imports that spend their time before the first script. The
house rules are in `docs/OPTIMIZATION-GRADING-SPEC.md` (`R52c` extended,
`R183`-`R184` new).

## Every line in the fullscreen player reads, on every cover

Reported with a screenshot: "the text mixes into the background", worst around
the song details. Reproduced on The Bends cover — a bright face on a dark
frame, the exact shape that breaks the rule the ink table used. The cover's
AVERAGE luminance is 0.24, so the player chose white ink; the field its
ambience paints is ~0.45, because the orbs and the bloom are screen-blended and
ADD light on top of that average. Every tier below the title measured
1.64:1–3.49:1 there and four were under 3:1 — the volume box was the worst at
1.64:1, which is an invisible `%`.

  * `npInk` gains the band between: a cover between `NP_FIELD_AS_IS` (0.20) and
    `NP_INK_FLIP` (0.42) keeps the WHITE table and gets a full-bleed scrim of
    its own colour mixed toward near-black — a scrim, not a panel, the same
    mechanism the bright band already used in the other direction. A cover dark
    enough for the field as it is still draws nothing at all.
  * The muted tiers move up one rung, and the top bar, the transport cluster
    and the seek readout take the glyph shadow the metadata block and the
    lyrics already had.
  * The chrome takes the CHOSEN table's colour instead of fixed greys: the top
    bar's icons and labels, and `VolumePct`, which draws on two surfaces (the
    fullscreen chrome and the player bar) where one fixed grey could only suit
    one of them. Over a music video the top bar keeps light greys — the picture
    is the field there, and the ink's polarity says nothing about it.

Measured from the rendered pixels (the computed ink against the field just
outside each text box, WCAG): **17 tiers on that cover, worst 6.98:1**, title
15.02:1 — where the worst was 1.64:1 and four tiers were below 3:1.

## The import pipeline: what can overlap, overlaps — and what cannot, does not

Measured by driving `finish_album` over one real album (Radiohead — OK Computer,
12 FLAC, 359 MB) with the provider caches cleared before every run:

| | before | after |
|---|---|---|
| press → first script | 63.3 s | 52.1 s |
| whole import, cold | 65.9 s | 64.0 s |
| re-import of the same album | 30.5 s | 19.3 s |

  * **The two file-writing steps run beside the four tag-writing ones.**
    Metadata (artist image, descriptions) and cover art write FILES; links,
    genres, advisory and instrumentals write TAGS. Different files, so the pair
    starts on one worker as soon as the genre step has settled the identity its
    lookup reads, and is joined before the chain — which is the first thing that
    needs it on disk.
  * **Auto tagging writes each file once.** Its six stages each used to save
    the whole container for themselves; the album pass now defers and flushes
    once per file. A failed flush wrote nothing and is reported by name.
  * **A tag write no longer costs a decode.** The CRC memo behind every CD log
    check and the audit is keyed on the container's own audio identity (FLAC's
    STREAMINFO MD5 — what a tag rewrite leaves and a re-rip changes) as well as
    size+mtime.
  * **Apple's spacing is not a queue.** `_apple_json` takes the 3 s interval
    under its lock and the request outside it, so one slow answer no longer
    stalls every other call behind it.
  * **The instrumentals pass runs per file in parallel** — LRCLIB's interval is
    0.4 s, where overlap wins.

One thing was tried and reverted, and the measurement is in the comment so
nobody re-applies it by pattern: fanning the ADVISORY pass out per file made
the album slower (24 s → 41 s), because Apple's interval is global and the
serial pass never reaches it — three seconds already separate its calls, and
the first track warms the album-level answers the rest reuse. The rule is the
provider's interval, not the shape of the loop.

## Where the time still goes

The same measurement says the largest single item left is the RateYourMusic
**links** step: 17–27 s of every run, spent in the Wayback ladder when RYM has
no cookie to answer with (`RYM_MAX_WALL` bounds one lookup at 20 s). Settings →
Discovery → "RateYourMusic archive fallback" turns it off; with a `rym_cookie`
configured the live site answers in ~1 s instead.

## Upgrading

Nothing to do: no key, no migration, no config change. Imports and re-imports
behave exactly as before, only sooner — same tags, same chain, same report.
