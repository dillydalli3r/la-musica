#!/usr/bin/env python3
"""The ONE cover-choice policy (mlo.cover_choice) — every rule, in order.

Pure and offline: candidates are dicts, the config is a dict, and nothing here
touches the network — `server.integrations` is imported only for the one check
that the finder's shipped source order IS the policy's (one place, two
readers).

What this pins, rule by rule:

  * a candidate that cannot be a cover at all is REJECTED with the reason —
    an empty answer (0 bytes), bytes that are not a decodable image, a provider
    that stated an error, and one below the cover target (the minimum
    `server.main._cover_metrics` reports and the grader enforces), whose
    rejection names that floor;
  * the release's own front cover beats a release-group stand-in, and a front
    cover beats the back;
  * a file at the target size beats an oversized one (nothing is rewarded for
    being downscaled) and an upscaled thumbnail loses to a clean image;
  * the configured `cover_sources` order breaks a tie, and so do the format,
    the square-aspect rule and, last of all, the provider's own order;
  * the winner says WHY it won and every loser says why it lost;
  * the notes state what each source did — including a keyed source that was
    skipped — and why nothing was chosen when nothing was;
  * the aim (the minimum, the target, the source order, the rules) is reported
    as data, and the default source order lives exactly once.

Run: python tools/test_cover_choice.py     (exit 0 pass, 1 fail)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import cover_choice as cc  # noqa: E402

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label):
    ok(got == want, label, f"got {got!r}, want {want!r}")


def has(hay, needle, label):
    ok(needle in hay, label, f"{needle!r} not in {hay!r}")


# The shipped cover configuration: exactly the crop/resize/target keys the
# grader and the write path read, so the policy cannot judge by other numbers.
CFG = {"cover_target_size": 1200, "cover_resize_enabled": True,
       "cover_enforce_size": True, "cover_force_exact_size": True,
       "cover_crop_enabled": True, "cover_crop_threshold": 0.0,
       "grader_strict_square_threshold": 0.0, "cover_enforce_square": True,
       "grade_check_cover_crop": True, "cover_jpeg_quality": 90,
       "cover_sources": []}


def cand(source, side, **kw):
    """A candidate as the finder hands one over: measured, never guessed."""
    row = {"source": source, "big": f"https://cdn.test/{source}/{side}.jpg",
           "small": f"https://cdn.test/{source}/{side}-small.jpg",
           "title": "Test Album", "artist": "Test Artist", "tracks": 10,
           "url": f"https://store.test/{source}", "width": side, "height": side,
           "format": "jpeg", "bytes": 200_000, "rank": 0}
    row.update(kw)
    return row


def by_url(ranked, url):
    return next((c for c in ranked if c.url == url), None)


CAA_RELEASE = "https://coverartarchive.org/release/11111111-1111-1111-1111-111111111111/front"
CAA_GROUP = "https://coverartarchive.org/release-group/22222222-2222-2222-2222-222222222222"
CAA_THUMB_250 = ("https://coverartarchive.org/release/"
                 "11111111-1111-1111-1111-111111111111/34025419985-250.jpg")
CAA_THUMB_FULL = ("https://coverartarchive.org/release/"
                  "11111111-1111-1111-1111-111111111111/34025419985.jpg")

# --------------------------------------------------------------------------- #
# 1. the aim: the minimum is the cover target, and the source order is one list
# --------------------------------------------------------------------------- #
print("\npolicy")
report = cc.policy_report(CFG)
eq(report["target"], 1200, "the target is the configured cover_target_size")
eq(report["minimum"], 1200, "the minimum IS that target (the floor the grader enforces)")
eq(report["sources"], list(cc.DEFAULT_SOURCE_ORDER),
   "an empty cover_sources means the shipped default order")
ok(len(report["rules"]) >= 8, "the policy reports its rules in prose", report["rules"])
eq(cc.policy_config({"cover_sources": ["deezer", "qobuz"]})["cover_sources"],
   ["deezer", "qobuz"], "a configured order wins wholesale")
eq(cc.policy_config({"cover_resize_enabled": False})["cover_minimum"], 0,
   "with resize off there is no floor to fall short of")

# The finder's own catalogue order is the SAME list, not a second copy.
from server import integrations as intg  # noqa: E402  (no network until called)
eq(intg.COV_SOURCE_PRIORITY, list(cc.DEFAULT_SOURCE_ORDER),
   "the finder's source priority IS the policy's default order")
eq(intg.COV_FALLBACK_SOURCES, list(cc.DEFAULT_SOURCE_ORDER),
   "and so is its offline fallback list")

# --------------------------------------------------------------------------- #
# 2. size: a clean 1200px cover beats a huge one, and says why
# --------------------------------------------------------------------------- #
print("\nsize")
huge = cand("applemusic", 3000, front=True, kind="front")
at_target = cand("qobuz", 1200, front=True, kind="front")
chosen, ranked, notes = cc.choose_covers([huge, at_target], CFG)
eq(chosen.url, at_target["big"], "the file at the target size wins over 3000px")
has(chosen.reasons[-1], "image size", "the deciding sentence names the size")
has(" ".join(by_url(ranked, huge["big"]).reasons),
    "3000×3000 is 1800px over the 1200px cover target",
    "the oversized candidate says what it lost on")
has(" ".join(by_url(ranked, at_target["big"]).reasons),
    "exactly the 1200px cover target", "the winner's own facts state its fit")

undersized = cand("deezer", 1000, front=True, kind="front")
smaller = cand("deezer", 600, front=True, kind="front")
# Both below the target, so the FLOOR does not reject them (no resize: the
# minimum is the target only while the write path resizes to it) — which is
# exactly the "undersized is acceptable, never upscaled" case.
no_floor = dict(CFG, cover_resize_enabled=False)
chosen, ranked, notes = cc.choose_covers([smaller, undersized], no_floor)
eq(chosen.url, undersized["big"], "the larger of two undersized images wins")
has(" ".join(by_url(ranked, undersized["big"]).reasons), "short of the 1200px target",
    "and its reason says how far short it is")
has(" ".join(by_url(ranked, smaller["big"]).reasons), "nothing is upscaled",
    "and the reason says an undersized image is never enlarged")
eq(cc.policy_config(no_floor)["cover_minimum"], 0,
   "with resize off, the target is a preference and not a floor")

unknown = cand("spotify", None, width=None, height=None, format=None, bytes=None)
chosen, ranked, notes = cc.choose_covers([unknown, at_target], CFG)
eq(chosen.url, at_target["big"], "an unmeasured image is neither rewarded nor rejected")
has(" ".join(by_url(ranked, unknown["big"]).reasons), "size unknown",
    "and it says the size was never probed")

# --------------------------------------------------------------------------- #
# 3. the release's own cover beats a release-group stand-in
# --------------------------------------------------------------------------- #
print("\nrelease vs stand-in")
own = cand("coverartarchive", 1200, big=CAA_RELEASE, front=True, kind="front",
           release_cover=True)
stand_in = cand("coverartarchive", 1200, big=CAA_GROUP, front=True, kind="front",
                release_cover=False)
chosen, ranked, notes = cc.choose_covers([stand_in, own], CFG)
eq(chosen.url, CAA_RELEASE, "the release's own front cover wins")
has(chosen.reasons[-1], "release's own cover", "and the deciding sentence says so")
has(" ".join(by_url(ranked, CAA_GROUP).reasons),
    "release-group stand-in", "the stand-in states what it is")
eq(intg.cover_url_labels(CAA_GROUP), (None, False),
   "a CAA release-group URL is read as a stand-in")
eq(intg.cover_url_labels(CAA_RELEASE), ("front", True),
   "a CAA release front URL is read as the release's own front cover")
eq(intg.cover_url_labels("https://cdn.test/x.jpg"), (None, None),
   "a store CDN URL states neither")

# --------------------------------------------------------------------------- #
# 4. front beats back, format, aspect and the provider's own order
# --------------------------------------------------------------------------- #
print("\nthe late tiers")
back = cand("deezer", 1200, kind="back", front=False)
front = cand("deezer", 1200, kind="front", front=True)
chosen, ranked, notes = cc.choose_covers([back, front], CFG)
eq(chosen.url, front["big"], "a labelled front cover beats a back cover")

png = cand("deezer", 1200, front=True, kind="front", format="png",
           big="https://cdn.test/deezer/1200.png")
jpg = cand("deezer", 1200, front=True, kind="front", format="jpeg",
           big="https://cdn.test/deezer/1200.jpg")
chosen, ranked, notes = cc.choose_covers([png, jpg], CFG)
eq(chosen.url, jpg["big"], "JPEG (the library's own cover format) beats PNG")
has(" ".join(by_url(ranked, png["big"]).reasons),
    "re-encoded to JPEG when it is written", "and the PNG says why it lost")

# Both at the target on their SHORTER side (what the square crop keeps), so
# only the aspect decides.
wide = cand("deezer", 1200, big="https://cdn.test/deezer/wide.jpg",
            width=1600, height=1200, front=True, kind="front")
square = cand("deezer", 1200, front=True, kind="front",
              big="https://cdn.test/deezer/square.jpg")
chosen, ranked, notes = cc.choose_covers([wide, square], CFG)
eq(chosen.url, square["big"], "a square image beats one the writer would crop")
has(" ".join(by_url(ranked, wide["big"]).reasons), "not square",
    "and the cropped candidate says what it loses")

# Otherwise identical rows: only the CONFIGURED order can separate them.
a = dict(cand("deezer", 1200, front=True, kind="front"), rank=0)
b = dict(cand("qobuz", 1200, front=True, kind="front"), rank=0)
order_cfg = dict(CFG, cover_sources=["qobuz", "deezer"])
chosen, ranked, notes = cc.choose_covers([a, b], order_cfg)
eq(chosen.url, b["big"], "the configured source order breaks the tie")
has(chosen.reasons[-1], "source order", "and the deciding sentence names it")
chosen, ranked, notes = cc.choose_covers([a, b], dict(CFG, cover_sources=["deezer", "qobuz"]))
eq(chosen.url, a["big"], "reversing the configured order reverses the pick")
chosen, ranked, notes = cc.choose_covers([a, b], dict(CFG, cover_sources=["bandcamp"]))
ok(chosen is not None, "a source the order does not name still yields a pick")
has(" ".join(chosen.reasons), "not in the configured cover source order",
    "and its reason says the source is not in the order")

# The provider's own order is the LAST tiebreak and nothing else.
second = dict(a, rank=1, big="https://cdn.test/deezer/second.jpg")
first = cc.choose_covers([second, dict(a, rank=0)], CFG)[0]
eq(first.url, a["big"], "the provider's first answer settles an otherwise exact tie")
has(first.reasons[-1], "provider's own order", "and the deciding sentence says so")

# --------------------------------------------------------------------------- #
# 5. rejections: the floor is named, and nothing is silently dropped
# --------------------------------------------------------------------------- #
print("\nrejections")
tiny = cand("deezer", 400, front=True, kind="front")
chosen, ranked, notes = cc.choose_covers([tiny, at_target], CFG)
tiny_row = by_url(ranked, tiny["big"])
ok(tiny_row.rejected != "", "a below-minimum candidate is rejected", tiny_row)
has(tiny_row.rejected, "below the minimum 1200×1200",
    "and its rejection names the floor")
eq(ranked[-1].url, tiny["big"], "a rejected candidate is still reported, last")
ok(chosen.url == at_target["big"], "and it is not what is chosen")
has("\n".join(notes), "400×400 is below the minimum 1200×1200",
    "the notes repeat the rejection")

# The probe is the ONLY evidence about these two, so nothing could be a cover
# here: no size either, and nothing the URL answered with.
empty = cand("deezer", None, width=None, height=None, bytes=0, format=None)
undecodable = cand("applemusic", None, width=None, height=None, bytes=4096, format="")
refused = dict(cand("spotify", 1200), error="HTTP 403 from the provider")
no_url = dict(cand("lastfm", 1200), big=None, small=None)
chosen, ranked, notes = cc.choose_covers([empty, undecodable, refused, no_url], CFG)
ok(chosen is None, "nothing is chosen when no candidate can be a cover")
for row, needle in ((empty, "answered 0 bytes"),
                    (undecodable, "not a JPEG/PNG/WebP image"),
                    (refused, "HTTP 403 from the provider"),
                    (no_url, "no image URL")):
    got = by_url(ranked, row.get("big")) or next(
        (c for c in ranked if c.source == row["source"]), None)
    ok(got is not None and needle in got.rejected, f"rejected: {needle}", got)
has("\n".join(notes), "nothing could be chosen",
    "and the notes say why nothing could be chosen")
# A candidate the PROVIDER sized, whose probe came back empty, is not rejected
# for it: the provider proved there is an image, and the failed probe is a
# failed enrichment — it is ranked, and the reason says where the size came from.
sized = cand("deezer", 1200, bytes=0, format=None)
sized_pick, sized_ranked, _ = cc.choose_covers([sized], CFG)
eq(sized_pick.url, sized["big"],
   "a provider-sized candidate with an empty probe is still a cover")
has(" ".join(by_url(sized_ranked, sized["big"]).reasons), "the size is the provider's own",
    "and its reason says the probe answered nothing")
ok(all(c.score == 0.0 for c in ranked), "a rejected candidate scores zero")
eq(cc.pick(ranked), None, "pick() finds nothing when everything is rejected")

# --------------------------------------------------------------------------- #
# 6. upscaled and thumbnail candidates
# --------------------------------------------------------------------------- #
print("\nthumbnails")
# A 250px thumbnail request that answers 1200x1200: the pixels were invented
# for it, and the clean 1200px image from the same source wins even though the
# two tie on every other tier.
upscaled = cand("coverartarchive", 1200, big=CAA_THUMB_250,
                release_cover=True, front=True, kind="front")
clean = cand("coverartarchive", 1200, big=CAA_RELEASE, release_cover=True,
             front=True, kind="front")
chosen, ranked, notes = cc.choose_covers([upscaled, clean], CFG)
eq(chosen.url, CAA_RELEASE, "a 250px URL answering 1200px loses to the clean image")
has(" ".join(by_url(ranked, CAA_THUMB_250).reasons), "upscaled",
    "and it is called what it is: upscaled")
has(chosen.reasons[-1], "thumbnail/upscale check",
    "the deciding sentence names the upscale check")
# A thumbnail-sized request that answers exactly what it asked for: a
# re-compressed copy, ranked below the same size without the request (no
# floor here — the target is a preference while the write path does not resize).
thumb = cand("coverartarchive", 250, big=CAA_THUMB_250, release_cover=True,
             front=True, kind="front")
plain = cand("coverartarchive", 250, big=CAA_THUMB_FULL, release_cover=True,
             front=True, kind="front")
chosen, ranked, notes = cc.choose_covers([thumb, plain], dict(CFG, cover_resize_enabled=False))
eq(chosen.url, CAA_THUMB_FULL, "a thumbnail-sized request loses to a plain 250px image")
has(" ".join(by_url(ranked, CAA_THUMB_250).reasons), "thumbnail URL",
    "a thumbnail-sized request is named as a re-compressed copy")
eq(cc.url_size_hint(thumb["big"]), 250, "the URL's own request size is read")
eq(cc.url_size_hint("https://cdn-images.dzcdn.net/images/cover/x/1000x1000-000000-80-0-0.jpg"),
   1000, "Deezer's largest request is read too")
eq(cc.url_size_hint("https://cdn.test/plain.jpg"), None,
   "a URL that states no size is not guessed at")

# --------------------------------------------------------------------------- #
# 7. the notes: what every source did, and the winner's own report
# --------------------------------------------------------------------------- #
print("\nnotes")
sources = [
    {"id": "covers.musichoarders.xyz", "status": "used", "count": 3},
    {"id": "coverartarchive", "status": "empty"},
    {"id": "fanarttv", "status": "needs_key", "detail": "needs an API key in Settings"},
    {"id": "lastfm", "status": "skipped", "detail": "switched off in the source catalogue"},
    {"id": "deezer", "status": "error", "detail": "429 Too Many Requests"},
]
chosen, ranked, notes = cc.choose_covers([at_target], CFG, sources=sources)
has("\n".join(notes), "covers.musichoarders.xyz: 3 candidate(s)",
    "a source that answered is reported with its count")
has("\n".join(notes), "fanarttv: skipped — needs an API key in Settings",
    "a keyed source that was skipped says so, with the key")
has("\n".join(notes), "lastfm: skipped — switched off in the source catalogue",
    "a source that was never asked says why")
has("\n".join(notes), "deezer: refused — 429 Too Many Requests",
    "a source that refused is reported as a refusal")
has("\n".join(notes), "coverartarchive: answered with no covers",
    "a source with nothing says so rather than being silent")
has("\n".join(notes), "1 candidate(s) could be a cover",
    "the kept/rejected counts are stated")

# --------------------------------------------------------------------------- #
# 8. the payload the surfaces share, and determinism
# --------------------------------------------------------------------------- #
print("\npayload")
payload = cc.cover_payload([huge, at_target, tiny], CFG, sources=sources,
                           provider="cov")
eq(payload["chosen"]["big"], at_target["big"], "the payload carries the winner")
eq(payload["candidate_count"], 3, "and counts every candidate")
eq(payload["rejected_count"], 1, "including the rejected ones")
eq(payload["provider"], "cov", "and who answered")
eq([c["big"] for c in payload["candidates"]][0], at_target["big"],
   "the candidates arrive best-first, the winner leading")
ok(payload["candidates"][0]["reasons"], "each row carries its reasons")
ok(all(set(("source", "big", "width", "height", "format", "rank", "reasons"))
       <= set(c) for c in payload["candidates"]),
   "every row carries what it was measured from")
ok("minimum" in payload["policy"] and "rules" in payload["policy"],
   "and the policy that ranked them")

again = cc.cover_payload([huge, at_target, tiny], CFG, sources=sources, provider="cov")
eq(again, payload, "the same candidates and config always rank identically")

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("All cover-choice checks passed.")
sys.exit(0)
