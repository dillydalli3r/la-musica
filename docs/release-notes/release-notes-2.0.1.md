# la musica 2.0.1 — no RateYourMusic left anywhere in the app

2.0.0 removed the two scraped hosts and the `RATEYOURMUSIC_*` vocabulary. A
sweep over the whole tree for what the removal left behind — a grading comment
that still named an RYM link as one of "two links graded", the import chain's
log line, tag help text, an AI prompt, a cookie parser branch, test fixtures
and the TUI spec — found each one and took it out. Nothing grades, fetches,
writes or displays a RateYourMusic link any more; the one wire that changes is
the AI genre prompt (used while `ai_genre_research` is on), which no longer
offers RateYourMusic as a taxonomy.

## Grading

- **ONE identity link is graded, and it is MusicBrainz's.** The grader's own
  comment claimed two links were graded and named a RateYourMusic release
  page as one of them; the code has graded the MusicBrainz release (falling
  back to its release group) alone since 2026-10-12, and the comment now says
  so.
- **A stored `grade_check_rym_links` grades nothing** — pinned in
  `tools/test_grading_paths.py` beside the same test the removed
  `grade_check_genre_order` switch has.
- **The fixtures stop carrying RYM.** `tools/fixtures/home-online.json` and
  `tools/fixtures/library-az.json` no longer list `RYM_LINK` among a track's
  issues, no longer carry `RATEYOURMUSIC_*` slots in a track's values, and no
  longer key a finding on the retired message — the surviving "Missing
  MusicBrainz release link (import from MusicBrainz)" row per album is the one
  a current server emits.

## Importing

- **The chain's log line and docstrings name what runs**: "links, metadata,
  cover art, then the configured scripts" — not "RateYourMusic links" — in
  `server/soulseek_auto.py`, with the same wording in `server/imports.py` and
  `server/pending_albums.py`.
- **`/api/mb/*` is described as it behaves** — MusicBrainz/LRCLIB only. The
  module docstring promised `/api/rym/*` routes that have not existed since
  2.0.0.

## The surfaces around the chains

- The `WEBRATING_SOURCE` tag help no longer offers "(MusicBrainz,
  RateYourMusic, …)" — MusicBrainz is the one web-rating source.
- The AI genre prompt told the model the app stores "genres RateYourMusic and
  MusicBrainz keep"; it now names MusicBrainz alone, the vocabulary the app
  can actually resolve.
- Cookie tooling: the parser's docstring described two credentials — the one
  is the yt-dlp jar — and `parse_cookie_file`'s `with_values` branch, which
  existed only to re-serve the RYM `Cookie` header, is DELETED (no caller
  used it). The check stubs that labeled a rating source "RateYourMusic" now
  say MusicBrainz.
- `docs/RUST-TUI-PROMPT.md` — the leftover 2.0.0's own notes flagged — stops
  listing RateYourMusic as a live service, drops `RATEYOURMUSIC_*` from the
  tag families and `rym_links` from the grading keys.
- Stale text in `.gitignore`, `docker-compose.yml`, `desktop/README.md` and
  `web/src/lib/ratings.ts` no longer names a credential, cache or step that
  cannot exist.

What stays, deliberately: the migration that DROPS retired config keys
(`rym_cookie`, `rym_links_auto`, `grade_check_rym_links`, …), the boot-time
removal of the retired page cache, and the historical records (the grading
spec's RETIRED/REMOVED rows, the release notes, `AGENTS.md`). Those are what
make an upgraded install clean — not the app carrying RYM data.

## Verification

Every Python suite in `tools/` and every Node suite runs on the frozen tree
with no failures (exit 2 = cannot run here counted as pass), together with the
web gate (`tsc -b`, `oxlint src`, `npm run build`, exit 0) and the checks for
the touched surfaces: `check_home_online.mjs` (26/26), `check_library_az.mjs`
(PASS), `check_rating_alignment.mjs` (10/10), and
`check_np_metadata_contrast.cjs` against a scratch server (271/271).

A scratch server (`MLO_MUSIC_FOLDER` on a synthetic library) serves
`/api/health`, `/api/config`, `/api/library` and `/api/home` with no RYM
string in any payload, and `check_versions.py v2.0.1` reports all copies and
the tag agreeing.
