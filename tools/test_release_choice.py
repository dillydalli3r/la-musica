"""The release-choice policy (`mlo/release_choice.py`) and the paths using it.

One policy decides which MusicBrainz edition every acquisition path takes —
"Add to library", the bulk auto-import, the wish worker and the artist watch —
so the rules are pinned here once: status, medium, completeness, the original
edition, the plain title and the country tie-breaker, plus determinism, the
caller's release-group type filter, and the strict rules the unattended paths
apply. Fixtures only: no network, no config file, no downloads.

Run: python tools/test_release_choice.py
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import config as mloconfig                      # noqa: E402
from mlo.config import DEFAULT_CONFIG                     # noqa: E402
from mlo import release_choice as rc                     # noqa: E402
from server import artist_watch, integrations as intg    # noqa: E402
from server import pending_albums, wishes                # noqa: E402


def cfg(**over):
    """A config with this policy's keys stated, so no case rides the real one."""
    c = {"auto_import_avoid_promo": True, "auto_import_require_country": True,
         "auto_import_medium_order": ["CD", "Vinyl", "Cassette", "Other", "Digital Media"],
         "prefer_release_country": "", "prefer_original_edition": True}
    c.update(over)
    return c


def rel(mbid, *, status="Official", country="US", fmt="CD", tracks=12,
        date="1997-01-20", title="Album", disambiguation="", media=None):
    """A MusicBrainz release fixture with the fields this policy reads."""
    if media is None:
        media = [{"format": fmt, "track-count": tracks}] if fmt else []
    return {"id": mbid, "title": title, "status": status, "country": country,
            "date": date, "disambiguation": disambiguation, "media": media}


def group(**over):
    """A release-group fixture (integrations.release_group_browse's shape)."""
    g = {"id": "rg-1", "title": "Album", "first_release_date": "1997-01-20",
         "primary_type": "Album", "secondary_types": []}
    g.update(over)
    return g


def order(rows, g=None, **kw):
    return [c.release_mbid for c in rc.rank_releases(g or group(), rows, **kw)]


def pick(rows, g=None, **kw):
    c = rc.choose_release(g or group(), rows, **kw)
    return c.release_mbid if c else None


# --------------------------------------------------------------------------- #
# 0. The shipped medium order IS the policy's intent: CD first, the other
#    physical media next, digital last (a digital edition carries no catalog
#    number and no pressing, so it is what a Soulseek folder matches least).
# --------------------------------------------------------------------------- #
assert DEFAULT_CONFIG["auto_import_medium_order"] == \
    ["CD", "Vinyl", "Cassette", "Other", "Digital Media"], \
    DEFAULT_CONFIG["auto_import_medium_order"]
assert DEFAULT_CONFIG["prefer_release_country"] == ""
assert DEFAULT_CONFIG["prefer_original_edition"] is True

# 1. Medium order decides first among otherwise equal editions: CD > vinyl >
#    cassette > (unlisted) > digital.
rows = [rel("dig", fmt="Digital Media"), rel("vinyl", fmt="Vinyl"),
        rel("cd", fmt="CD"), rel("tape", fmt="Cassette")]
assert order(rows, cfg=cfg()) == ["cd", "vinyl", "tape", "dig"], order(rows, cfg=cfg())
assert pick(rows, cfg=cfg()) == "cd"

# Unknown medium labels: a format the order does not name ranks after every
# configured one (and keeps its own date order among its peers) …
rows = [rel("md", fmt="Minidisc"), rel("dig", fmt="Digital Media"), rel("cd", fmt="CD")]
assert order(rows, cfg=cfg()) == ["cd", "dig", "md"], order(rows, cfg=cfg())
# … while a label the user DOES configure keeps the position they gave it: a
# cassette-first order makes the cassette the pick over a CD.
assert pick([rel("cd", fmt="CD"), rel("tape", fmt="Cassette")],
            cfg=cfg(auto_import_medium_order=["Cassette", "CD"])) == "tape"

# A medium embedded in another format name still matches its label ("8cm CD",
# "HDCD" are CDs; MusicBrainz spells them that way).
assert pick([rel("vinyl", fmt="Vinyl"), rel("eight", fmt="8cm CD")], cfg=cfg()) == "eight"
# A multi-medium edition is scored by its BEST medium.
assert pick([rel("vinyl", fmt="Vinyl"),
             rel("cddvd", fmt="", media=[{"format": "DVD", "track-count": 2},
                                         {"format": "CD", "track-count": 12}])],
            cfg=cfg()) == "cddvd"

# 2. Status: official beats an unstated status, which beats a
#    withdrawn/expired edition, which beats a promotion, which beats a bootleg.
rows = [rel("promo", status="Promotion"), rel("boot", status="Bootleg"),
        rel("withdrawn", status="Withdrawn"), rel("plain", status=""),
        rel("official", status="Official")]
assert order(rows, cfg=cfg()) == ["official", "plain", "withdrawn", "promo", "boot"], \
    order(rows, cfg=cfg())

# A bootleg never beats an official edition, whatever its medium or date …
assert pick([rel("boot", status="Bootleg", fmt="CD", date="1997-01-20"),
             rel("official", status="Official", fmt="Vinyl", date="2011-01-01")],
            cfg=cfg()) == "official"
# … and when nothing official exists it IS the pick, with a reason that says
# exactly that — while the unattended paths (strict) still refuse it.
solo = [rel("boot", status="Bootleg", tracks=3, date="1999-01-01")]
lone = rc.choose_release(group(), solo, cfg())
assert lone.release_mbid == "boot"
assert any("no official edition exists" in r for r in lone.reasons), lone.reasons
assert any("auto_import_avoid_promo" in r for r in lone.reasons), lone.reasons
assert lone.eligible is False
assert rc.choose_release(group(), solo, cfg(), strict=True) is None
assert intg.pick_release(solo, cfg()) is None          # the auto-import view

# A promotion is only taken when it is all there is, and the same reason shows.
lone = rc.choose_release(group(), [rel("promo", status="Promotion")], cfg())
assert lone.eligible is False and any("no official edition exists" in r
                                      for r in lone.reasons), lone.reasons

# 3. Completeness: a release short of the release group's own track count is
#    penalised, so a 1-track promo can never win over the full album — even
#    when both are official (status is not doing the work here).
rows = [rel("sampler", tracks=1, title="Sampler"),
        rel("album", tracks=12)]
assert pick(rows, cfg=cfg()) == "album", order(rows, cfg=cfg())
rows = [rel("promo", status="Promotion", tracks=1, date="1996-01-01"),
        rel("album", tracks=12, date="1997-01-20")]
assert pick(rows, cfg=cfg()) == "album"
# A stated release-group count wins over the fullest edition offered.
assert pick([rel("full", tracks=12), rel("short", tracks=6)],
            group(track_count=12), cfg=cfg()) == "full"
# A partial edition says so.
short = rc.choose_release(group(), [rel("full", tracks=12), rel("short", tracks=6)], cfg())
whole = [c for c in rc.rank_releases(group(), [rel("full", tracks=12),
                                               rel("short", tracks=6)], cfg())]
assert any("short of the fullest edition offered" in r
           for c in whole for r in c.reasons), whole

# 4. The ORIGINAL edition beats a later reissue/deluxe — the group's first
#    release date is the reference, not the earliest edition in the list.
original, deluxe = rel("orig", date="1997-01-20"), rel("deluxe", date="2011-05-01")
assert pick([deluxe, original], cfg=cfg()) == "orig"
# … unless the later one is materially more complete.
heavy = rel("deluxe24", date="2011-05-01", tracks=24)
assert pick([rel("orig", tracks=12), heavy], cfg=cfg()) == "deluxe24"
assert any("short of the fullest edition offered" in r
           for c in rc.rank_releases(group(), [rel("orig", tracks=12), heavy], cfg())
           if c.release_mbid == "orig" for r in c.reasons)
# Among editions of the SAME year the one stating its date in full wins (the
# album folder is named after this date) — and an earlier year still beats a
# later, fully-dated one.
assert pick([rel("year", date="1983"), rel("full", date="1983-09-13")],
            group(first_release_date="1983-09-13"), cfg=cfg()) == "full"
assert pick([rel("y83", date="1983"), rel("full94", date="1994-03-01")],
            group(first_release_date="1983-09-13"), cfg=cfg()) == "y83"
# An edition with no date at all ranks after every dated one.
assert pick([rel("undated", date=""), rel("dated", date="1997-01-20")], cfg=cfg()) == "dated"
# A pressing that predates the group's stated first date is still the original.
assert pick([rel("early", date="1996-12-01"), rel("later", date="1997-01-20")],
            cfg=cfg()) == "early"

# 5. prefer_release_country is a TIE-BREAKER and nothing else: with everything
#    else equal it decides …
rows = [rel("gb", country="GB"), rel("us", country="US")]
assert pick(rows, cfg=cfg(prefer_release_country="US")) == "us"
assert pick(rows, cfg=cfg(prefer_release_country="gb")) == "gb"      # case-insensitive
assert pick(rows, cfg=cfg(prefer_release_country="")) in ("gb", "us")  # no preference
# … and it can never outvote a better medium or a fuller track list.
assert pick([rel("us-vinyl", country="US", fmt="Vinyl"),
             rel("gb-cd", country="GB", fmt="CD")],
            cfg=cfg(prefer_release_country="US")) == "gb-cd"
assert pick([rel("us-short", country="US", tracks=6),
             rel("gb-full", country="GB", tracks=12)],
            cfg=cfg(prefer_release_country="US")) == "gb-full"
# The reasons name the preference when it is configured, and the losing side
# says which country it has.
us = rc.choose_release(group(), [rel("gb", country="GB"), rel("us", country="US")],
                       cfg(prefer_release_country="US"))
assert any("preferred release country" in r for r in us.reasons), us.reasons
assert any("ranked above" in r for r in us.reasons), us.reasons

# 6. prefer_original_edition: a clean edition (MusicBrainz states it in the
#    title or the disambiguation comment) sorts below the original — and with
#    the key OFF it is ranked like any other edition, so the tie-breaker
#    decides (US over GB here).
clean = rel("clean", title="Album (Clean)", date="2011-05-01", country="US")
plain = rel("plain", title="Album", date="2011-05-01", country="GB")
assert pick([clean, plain], cfg=cfg(prefer_release_country="US")) == "plain"
assert pick([clean, plain],
            cfg=cfg(prefer_release_country="US", prefer_original_edition=False)) == "clean"
# A clean edition's reason says the rule it lost to (or that it was not
# penalised, when the key is off).
clean_lost = [c for c in rc.rank_releases(group(), [clean, plain],
                                          cfg(prefer_release_country="US"))
              if c.release_mbid == "clean"][0]
assert any("prefer_original_edition" in r for r in clean_lost.reasons), clean_lost.reasons
assert any("prefer_original_edition is off" in r
           for c in rc.rank_releases(group(), [clean, plain],
                                     cfg(prefer_original_edition=False)) if c.release_mbid == "clean"
           for r in c.reasons)
# "Edited" in the disambiguation comment counts too.
assert pick([rel("edited", disambiguation="edited version", date="2011-05-01"),
             rel("plain", date="2011-05-01")], cfg=cfg()) == "plain"

# 7. A plain release beats a disambiguated/parenthesised one when everything
#    else ties.
assert pick([rel("annotated", disambiguation="1997 US pressing"),
             rel("plain")], cfg=cfg()) == "plain"
assert any('disambiguation "1997 US pressing"' in r
           for c in rc.rank_releases(group(), [rel("a", disambiguation="1997 US pressing")], cfg())
           for r in c.reasons)

# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
rows = [rel("a"), rel("b", fmt="Vinyl"), rel("c", status=""), rel("d", tracks=6),
        rel("e", country="")]
first = rc.rank_releases(group(), copy.deepcopy(rows), cfg())
again = rc.rank_releases(group(), copy.deepcopy(rows), cfg())
assert [(c.release_mbid, c.score, c.reasons) for c in first] == \
    [(c.release_mbid, c.score, c.reasons) for c in again]
# Identical editions tie on every rule, and a tie keeps the order they arrived
# in — so the pick is stable rather than dependent on dict ordering.
twin_a, twin_b = rel("twin-a"), rel("twin-b")
assert pick([twin_a, twin_b], cfg=cfg()) == "twin-a"
assert pick([twin_b, twin_a], cfg=cfg()) == "twin-b"
tied = rc.rank_releases(group(), [twin_a, twin_b], cfg=cfg())
assert tied[0].score == tied[1].score, (tied[0].score, tied[1].score)
assert any("tied with" in r for r in tied[0].reasons), tied[0].reasons
# The score agrees with the order: a better pick never scores lower.
assert all(first[i].score >= first[i + 1].score for i in range(len(first) - 1)), \
    [(c.release_mbid, c.score) for c in first]

# --------------------------------------------------------------------------- #
# The caller's release-group TYPE (mlo.naming's vocabulary)
# --------------------------------------------------------------------------- #
single = group(id="rg-single", title="Song", primary_type="Single",
               first_release_date="2001-01-01")
single_rows = [rel("s1", title="Song", date="2001-01-01")]
# A watch (or a page) asking for albums is not answered with a single, however
# well the single ranks on medium.
assert rc.choose_release(single, single_rows, cfg(), primary_type="album") is None
assert rc.choose_release(single, single_rows, cfg(), primary_type="single").release_mbid == "s1"
refused = rc.rank_releases(single, single_rows, cfg(), primary_type="album")[0]
assert refused.eligible is False
assert "release group type is not the type asked for" in refused.reasons[0], refused.reasons
# The pair form the search route takes ("primary_type"/"secondary_type") works
# the same way, and an unrecognized name selects nothing rather than acting as
# "no filter".
assert rc.choose_release(single, single_rows, cfg(),
                         primary_type="single", secondary_type="") is not None
assert rc.choose_release(single, single_rows, cfg(), primary_type="nonsense") is None
# A secondary type is a QUALIFIER and decides the match: the live album is
# found by "live", not by "album" alone.
live = group(primary_type="Album", secondary_types=["Live"])
live_rows = [rel("l1", title="Live at Somewhere", date="1997-01-20")]
assert rc.choose_release(live, live_rows, cfg(), wanted_types=["live"]) is not None
assert rc.choose_release(live, live_rows, cfg(), primary_type="album") is None
# The type the caller asked about is echoed, and the choice reads the type from
# the release group or from the row itself.
body = rc.choice_payload("rg-1", group(primary_type="Album", secondary_types=["Live"]),
                         live_rows, cfg(), wanted_types=["live"])
assert body["release_group"]["primary_type"] == "Album"
assert body["release_group"]["secondary_types"] == ["Live"]
row_typed = dict(live_rows[0], primary_type="Album", secondary_types=["Live"])
assert rc.choose_release({}, [row_typed], cfg(), wanted_types=["live"]) is not None
# The watch's own matcher is this one (one implementation, two callers).
for want in (["album"], ["live"], ["single"], [], ["nonsense"]):
    for g in (group(), live, single):
        assert artist_watch.type_matches(g["primary_type"], g["secondary_types"], want) == \
            rc.type_matches(g["primary_type"], g["secondary_types"], want)

# --------------------------------------------------------------------------- #
# The strict rules the unattended paths apply (and only they)
# --------------------------------------------------------------------------- #
# A country-less edition is skipped by auto-import while require-country is on,
# and ranked normally when the user turns that off.
rows = [rel("nocountry", country=""), rel("country", country="GB")]
assert pick(rows, cfg=cfg(), strict=True) == "country"
assert pick(rows, cfg=cfg(auto_import_require_country=False), strict=True) == "nocountry"
assert pick(rows, cfg=cfg()) == "nocountry"     # the report still ranks it

# --------------------------------------------------------------------------- #
# The payload the UI reads: group, pick, alternatives, policy
# --------------------------------------------------------------------------- #
many = [rel(f"r{n}", date=f"1997-01-{n:02d}") for n in range(1, 26)]
body = rc.choice_payload("rg-1", group(), many, cfg())
assert body["release_group_mbid"] == "rg-1"
assert body["release_group"] == {"title": "Album", "first_release_date": "1997-01-20",
                                 "primary_type": "Album", "secondary_types": [],
                                 "track_count": 12}, body["release_group"]
assert set(body["chosen"]) == {"release_mbid", "title", "date", "country", "status",
                               "media", "track_count", "disambiguation", "score",
                               "eligible", "reasons"}, sorted(body["chosen"])
assert body["chosen"]["release_mbid"] == body["candidates"][0]["release_mbid"]
assert len(body["candidates"]) == rc.CANDIDATE_LIMIT          # capped at ~20
assert body["candidates"][0]["media"] == ["CD"]
assert body["policy"]["medium_order"] == cfg()["auto_import_medium_order"]
assert body["policy"]["preferred_country"] == ""
assert body["policy"]["prefer_original_edition"] is True
assert body["policy"]["status_order"] == ["official", "promotion", "bootleg"]
assert len(body["policy"]["rules"]) == 7, body["policy"]["rules"]

# `prefer` is the user's own edition: it is the pick, and its reasons say the
# user asked for it; an id this group does not carry is reported as no pick.
body = rc.choice_payload("rg-1", group(), [rel("a"), rel("b", fmt="Vinyl")],
                         cfg(), prefer="b")
assert body["chosen"]["release_mbid"] == "b"
assert any("you asked for this release" in r for r in body["chosen"]["reasons"])
assert rc.choice_payload("rg-1", group(), [rel("a")], cfg(), prefer="zz")["chosen"] is None
# A group with no editions at all has no pick and no candidates.
empty = rc.choice_payload("rg-1", group(), [], cfg())
assert empty["chosen"] is None and empty["candidates"] == []

# A release_lookup payload (media = a flat track list, formats in
# medium_formats) is read the same way as a browse row: this is the shape the
# importer and the wish worker hold.
lookup = {"id": "lk", "title": "Album", "status": "Official", "country": "US",
          "date": "1997-01-20", "medium_formats": ["CD"],
          "media": [{"position": i, "title": f"T{i}"} for i in range(1, 13)]}
assert rc.track_count(lookup) == 12 and rc.media_formats(lookup) == ["CD"]
assert pick([lookup], cfg=cfg()) == "lk"

# --------------------------------------------------------------------------- #
# The acquisition paths use THIS policy
# --------------------------------------------------------------------------- #
# Stubbed at the MusicBrainz TRANSPORT instead of at release_group_browse, so
# the page this builds, the ids it resolves and the editions it queues all go
# through the real code under test. MusicBrainz ids are real UUIDs here because
# integrations._mbid only accepts those.
def _uuid(name):
    h = hashlib.md5(name.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


MBID = {n: _uuid(n) for n in ("rg-1", "rg-single", "later", "cd", "vinyl",
                             "bootleg", "s1", "ar-1", "digital", "promo")}
NAME = {v: k for k, v in MBID.items()}

RG_PAYLOAD = {"id": MBID["rg-1"], "title": "Album", "first-release-date": "1997-01-20",
              "primary-type": "Album", "secondary-types": []}
RAW = [dict(rel(MBID["later"], date="2011-05-01"), title="Album"),
       dict(rel(MBID["cd"], date="1997-01-20"), title="Album"),
       dict(rel(MBID["vinyl"], date="1997-01-20", fmt="Vinyl"), title="Album"),
       dict(rel(MBID["bootleg"], status="Bootleg", date="1997-01-20"), title="Album")]
RELEASES = {r["id"]: dict(
    r, release_group_id=MBID["rg-1"], artists=[{"name": "A", "mbid": MBID["ar-1"]}],
    label="L", catalog_number="CAT-1", catalog_numbers=["CAT-1"],
    medium=r["media"][0]["format"], medium_count=1,
    medium_formats=[r["media"][0]["format"]],
    media=[{"position": n, "title": f"T{n}"} for n in range(1, 13)],
    originaldate="1997-01-20", release_type="album", primary_type="Album",
    secondary_types=[]) for r in RAW}
SINGLE_PAYLOAD = {"id": MBID["rg-single"], "title": "Song",
                  "first-release-date": "2001-01-01",
                  "primary-type": "Single", "secondary-types": []}
SINGLE_RAW = [dict(rel(MBID["s1"], title="Song", date="2001-01-01"), title="Song")]

# The paths under test load the app config themselves (integrations.
# _release_cfg), so the fixture config is what they read — the test depends on
# no user setting and writes no config file.
_real = (intg.mb_get_cached, intg._browse_collect, intg.release_lookup,
         intg.artist_browse, wishes.owned_mbids, mloconfig.load_config)
mloconfig.load_config = lambda *a, **kw: cfg()


def _transport(payload, raw):
    intg.mb_get_cached = lambda endpoint, params=None, **kw: dict(payload)
    intg._browse_collect = lambda endpoint, extra, list_key, count_key, limit=300, offset=0: (
        [dict(r) for r in raw], len(raw), len(raw))


def _lookup(mbid):
    if mbid not in RELEASES:
        raise KeyError(mbid)          # a group id is not a release id
    return RELEASES[mbid]


def names(ids):
    return [NAME[i] for i in ids]


try:
    _transport(RG_PAYLOAD, RAW)
    intg.release_lookup = _lookup
    intg.artist_browse = lambda mbid, limit=500, offset=0: {
        "release_groups": [{"id": MBID["rg-1"], "title": "Album", "primary_type": "Album",
                            "secondary_types": [], "first_release_date": "1997-01-20"}]}
    wishes.owned_mbids = lambda cfg=None: set()   # no library in this test

    # The release-group page lists the editions in the policy's own order, so
    # its FIRST row is the edition every path would queue: the 1997 CD, then
    # the 2011 CD reissue, then the 1997 vinyl, and the bootleg last. The order
    # IS the policy: the medium order (rule 2 — CD first) outvotes the
    # original-edition date (rule 4), which decides among same-medium editions.
    page = intg.release_group_browse(MBID["rg-1"])
    assert names([r["id"] for r in page["releases"]]) == ["cd", "later", "vinyl", "bootleg"], \
        names([r["id"] for r in page["releases"]])
    assert [r["score"] for r in page["releases"]] == sorted(
        [r["score"] for r in page["releases"]], reverse=True)
    assert page["releases"][0]["reasons"] and "disambiguation" in page["releases"][0]
    assert page["releases"][0]["disambiguation"] == ""

    # group_targets: one policy pick, with its score and reasons; "all" is
    # every ELIGIBLE edition (the bootleg is not one).
    rows, err = intg.group_targets(MBID["rg-1"], "best")
    assert err is None and names([r["mbid"] for r in rows]) == ["cd"], (rows, err)
    assert rows[0]["reasons"] and isinstance(rows[0]["score"], float)
    rows_all, err_all = intg.group_targets(MBID["rg-1"], "all")
    assert err_all is None and names([r["mbid"] for r in rows_all]) == \
        ["cd", "later", "vinyl"], names([r["mbid"] for r in rows_all])

    # A watch's type filter rides INTO the choice: an album watch is never
    # answered with a single, whatever the medium says.
    _transport(SINGLE_PAYLOAD, SINGLE_RAW)
    intg.release_lookup = lambda mbid: dict(RELEASES.get(mbid, SINGLE_RAW[0]),
                                            release_group_id=MBID["rg-single"])
    rows, err = intg.group_targets(MBID["rg-single"], "best", types=["album"])
    assert rows == [] and err == "release-group type not requested (Single)", (rows, err)
    rows, err = intg.group_targets(MBID["rg-single"], "best", types=["single"])
    assert err is None and names([r["mbid"] for r in rows]) == ["s1"], (rows, err)
    _transport(RG_PAYLOAD, RAW)
    intg.release_lookup = _lookup

    # resolve_release: a GROUP id resolves to the policy's edition (the 1997 CD,
    # not the 2011 reissue and not the bootleg).
    release, rid = intg.resolve_release(MBID["rg-1"])
    assert NAME.get(rid) == "cd" and release["id"] == MBID["cd"], (rid, release)
    # A release id resolves to itself, untouched.
    assert intg.resolve_release(MBID["vinyl"])[1] == MBID["vinyl"]

    # auto_import_targets: a group queues the pick, an artist one pick per
    # release group — the same function the "Add to library" route and the
    # bulk job share.
    targets, skipped = intg.auto_import_targets(MBID["rg-1"], "release_group", "best")
    assert names([t["mbid"] for t in targets]) == ["cd"], (targets, skipped)
    targets, skipped = intg.auto_import_targets(MBID["ar-1"], "artist", "best")
    assert names([t["mbid"] for t in targets]) == ["cd"], (targets, skipped)
    # "all" keeps every eligible edition, best first.
    targets, _skip = intg.auto_import_targets(MBID["rg-1"], "release_group", "all")
    assert names([t["mbid"] for t in targets]) == ["cd", "later", "vinyl"], targets
    # pick_releases/pick_release keep the auto-import view: the eligible ones,
    # best first, as the payloads they were handed.
    assert names([r["id"] for r in intg.pick_releases(RAW)]) == ["cd", "later", "vinyl"]
    assert NAME[intg.pick_release(RAW)["id"]] == "cd"

    # The artist watch's one seam: the edition it queues is the policy's pick,
    # WITH the watch's own type filter applied to the choice.
    queued = []
    real_create = pending_albums.create
    pending_albums.create = lambda release, cfg, **kw: (
        queued.append(NAME[release["id"]]) or {"wish_id": None, "created": False})
    try:
        release, err, chosen = artist_watch._release_for(MBID["rg-1"], cfg(), types=["album"])
        assert err is None and NAME[release["id"]] == "cd", (release, err)
        assert NAME[chosen["mbid"]] == "cd" and chosen["reasons"], chosen
        assert artist_watch.queue_release(release, cfg(), title="Album") is not None
        assert queued == ["cd"], queued
        # The type filter is the watch's: an album watch is refused a single
        # group outright, so nothing is queued from it.
        _transport(SINGLE_PAYLOAD, SINGLE_RAW)
        intg.release_lookup = lambda mbid: dict(RELEASES.get(mbid, SINGLE_RAW[0]),
                                               release_group_id=MBID["rg-single"])
        release, err, chosen = artist_watch._release_for(MBID["rg-single"], cfg(),
                                                         types=["album"])
        assert release is None and chosen is None and \
            err == "release-group type not requested (Single)", (release, err)
        assert queued == ["cd"], queued
    finally:
        pending_albums.create = real_create

    # GET /api/mb/release-choice: the route the UI reads. Mounted on its own
    # app here (server.main registers the router), so this is the module's own
    # behaviour — one browse, the policy's body, and the failures a page has to
    # tell apart.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server import api_choice

    app = FastAPI()
    app.include_router(api_choice.router)
    client = TestClient(app)

    assert client.get("/api/mb/release-choice").status_code == 400
    assert client.get("/api/mb/release-choice",
                      params={"release_group_mbid": "not-a-uuid"}).status_code == 400
    _transport({}, [])
    assert client.get("/api/mb/release-choice",
                      params={"release_group_mbid": MBID["rg-1"]}).status_code == 404
    _transport(RG_PAYLOAD, RAW)
    got = client.get("/api/mb/release-choice", params={"release_group_mbid": MBID["rg-1"]})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["release_group_mbid"] == MBID["rg-1"]
    assert body["release_group"] == {"title": "Album", "first_release_date": "1997-01-20",
                                     "primary_type": "Album", "secondary_types": [],
                                     "track_count": 12}, body["release_group"]
    assert NAME[body["chosen"]["release_mbid"]] == "cd"
    assert NAME[body["candidates"][0]["release_mbid"]] == "cd"
    assert body["candidates"][0]["eligible"] is True
    assert body["candidates"][0]["reasons"]
    assert body["policy"]["medium_order"] == ["CD", "Vinyl", "Cassette", "Other",
                                              "Digital Media"]
    # The type the caller asked about is echoed, and a single is not offered
    # where an album was asked for.
    _transport(SINGLE_PAYLOAD, SINGLE_RAW)
    album_ask = client.get("/api/mb/release-choice",
                           params={"release_group_mbid": MBID["rg-single"],
                                   "primary_type": "album"}).json()
    assert album_ask["chosen"] is None, album_ask["chosen"]
    assert album_ask["release_group"]["primary_type"] == "Single"
    assert "not the type asked for" in album_ask["candidates"][0]["reasons"][0]
    single_ask = client.get("/api/mb/release-choice",
                            params={"release_group_mbid": MBID["rg-single"],
                                    "primary_type": "single"}).json()
    assert NAME[single_ask["chosen"]["release_mbid"]] == "s1"
    # `prefer` overrides the pick and says so; an id this group does not carry
    # is a 404 rather than a silent substitution.
    _transport(RG_PAYLOAD, RAW)
    forced = client.get("/api/mb/release-choice",
                        params={"release_group_mbid": MBID["rg-1"],
                                "prefer": MBID["vinyl"]})
    assert forced.status_code == 200
    assert NAME[forced.json()["chosen"]["release_mbid"]] == "vinyl"
    assert any("you asked for this release" in r
               for r in forced.json()["chosen"]["reasons"])
    # "-" is the "no override" sentinel: the policy's own pick comes back.
    dash = client.get("/api/mb/release-choice",
                      params={"release_group_mbid": MBID["rg-1"], "prefer": "-"})
    assert dash.status_code == 200 and NAME[dash.json()["chosen"]["release_mbid"]] == "cd"
    missing = client.get("/api/mb/release-choice",
                         params={"release_group_mbid": MBID["rg-1"],
                                 "prefer": _uuid("not-in-this-group")})
    assert missing.status_code == 404, missing.text
    # A MusicBrainz outage is a 502, never "no such release group".
    _real_get = intg.mb_get_cached
    intg.mb_get_cached = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        assert client.get("/api/mb/release-choice",
                          params={"release_group_mbid": MBID["rg-1"]}).status_code == 502
    finally:
        intg.mb_get_cached = _real_get
finally:
    (intg.mb_get_cached, intg._browse_collect, intg.release_lookup,
     intg.artist_browse, wishes.owned_mbids, mloconfig.load_config) = _real

# --------------------------------------------------------------------------- #
# Country lists: a release group is released in MANY countries at once
# --------------------------------------------------------------------------- #
# A release group's editions are scattered across countries and each carries
# its own release events, while MusicBrainz's singular `country` states only
# the FIRST of them. Both pages therefore show the whole list — country, the
# event's date, the edition that carries it, and whether it is the user's own
# `prefer_release_country` — and never invent an event MusicBrainz does not
# state. Stubbed at the transport, so these payloads come from the real
# builders.
def _event(name, code, date):
    """One MusicBrainz release event: `area` + the event's date. An area with
    no ISO code (a historic area) carries none."""
    area = {"name": name}
    if code:
        area["iso-3166-1-codes"] = [code]
    return {"area": area, "date": date}


MULTI_RAW = [
    {**rel(MBID["cd"], date="1997-01-20", country="US"), "title": "Album",
     "release-events": [_event("United States", "US", "1997-01-20")]},
    {**rel(MBID["later"], date="2011-05-01", country="GB"), "title": "Album",
     "release-events": [_event("United Kingdom", "GB", "2011-05-01"),
                        _event("Europe", "XE", "2011-05-01")]},
    {**rel(MBID["vinyl"], date="1997-01-20", country="JP", fmt="Vinyl"), "title": "Album",
     "release-events": [_event("Japan", "JP", "1997-01-20"),
                        # the same (country, date) the CD carries: one entry,
                        # credited to the first-ranked edition that has it
                        _event("United States", "US", "1997-01-20")]},
    {**rel(MBID["bootleg"], status="Bootleg", date="1999-01-01", country="US"),
     "title": "Album", "release-events": []},
]

_real_cfg = mloconfig.load_config
try:
    mloconfig.load_config = lambda *a, **kw: cfg(prefer_release_country="GB")
    _transport(RG_PAYLOAD, MULTI_RAW)
    page = intg.release_group_browse(MBID["rg-1"])
    assert [(c["country"], c["code"], c["date"], c["preferred"]) for c in page["countries"]] == [
        ("Japan", "JP", "1997-01-20", False),
        ("United States", "US", "1997-01-20", False),      # deduped across editions
        ("Europe", "XE", "2011-05-01", False),
        ("United Kingdom", "GB", "2011-05-01", True),      # the preference, MARKED
    ], page["countries"]
    # every entry says which edition carries the event, and the first-ranked
    # edition carrying it wins (the CD, not the same-day vinyl)
    assert {c["release_id"] for c in page["countries"]} == {
        MBID["cd"], MBID["later"], MBID["vinyl"]}, page["countries"]
    assert next(c for c in page["countries"] if c["code"] == "US")["release_id"] == MBID["cd"]
    # …and no release was dropped from the page by the country work
    assert len(page["releases"]) == 4, page["releases"]

    # One country: one entry, still naming its edition, still no invented one.
    _transport(RG_PAYLOAD, [{**rel(MBID["cd"], date="1997-01-20"), "title": "Album",
                             "release-events": [_event("United States", "US", "1997-01-20")]}])
    one = intg.release_group_browse(MBID["rg-1"])
    assert one["countries"] == [{"country": "United States", "code": "US",
                                 "date": "1997-01-20", "preferred": False,
                                 "release_id": MBID["cd"]}], one["countries"]
    assert len(one["releases"]) == 1, one["releases"]

    # No events at all: an EMPTY list — MusicBrainz stated nothing, so nothing
    # is invented and the page prints no country field (its own guard is
    # `events.length`); the edition itself still lists.
    _transport(RG_PAYLOAD, [{**rel(MBID["cd"], date="1997-01-20"), "title": "Album",
                             "release-events": []}])
    none = intg.release_group_browse(MBID["rg-1"])
    assert none["countries"] == [], none["countries"]
    assert len(none["releases"]) == 1, none["releases"]

    # A release shows its OWN events; its singular `country` (the value the
    # auto-import policy keys on) is left exactly as MusicBrainz stated it, and
    # a placeless event states no country, so it is not one.
    _transport({
        "id": MBID["cd"], "title": "Album", "date": "1997-01-20", "country": "US",
        "barcode": "", "status": "Official",
        "release-group": {"id": MBID["rg-1"], "primary-type": "Album",
                          "secondary-types": [], "first-release-date": "1997-01-20"},
        "label-info": [], "media": [],
        "release-events": [_event("United States", "US", "1997-01-20"),
                           {"area": None, "date": "1997"},
                           _event("Europe", "XE", "1997-04-01")],
    }, [])
    mloconfig.load_config = lambda *a, **kw: cfg(prefer_release_country="US")
    lookup = intg.release_lookup(MBID["cd"])
    assert lookup["country"] == "US", lookup["country"]
    assert lookup["countries"] == [
        {"country": "United States", "code": "US", "date": "1997-01-20", "preferred": True},
        {"country": "Europe", "code": "XE", "date": "1997-04-01", "preferred": False},
    ], lookup["countries"]
    # no preference configured: every entry is unmarked, never a wrong mark
    mloconfig.load_config = lambda *a, **kw: cfg()
    assert [c["preferred"] for c in intg.release_lookup(MBID["cd"])["countries"]] == \
        [False, False]
finally:
    mloconfig.load_config = _real_cfg
    _transport(RG_PAYLOAD, RAW)

print("ok  release/release-group country lists: every release event with its "
      "date, its carrying edition and the preferred-country mark — multi, "
      "single and none")

# --------------------------------------------------------------------------- #
# The UI renders THESE payloads
# --------------------------------------------------------------------------- #
# tools/check_release_choice.mjs is the web panel's own checker (the real
# component through Vite) and takes payload JSONs as arguments, so the panel is
# checked against the policy's REAL output instead of a hand-written fixture and
# the two halves cannot drift. Skipped with a note — never a failure — when node
# or the checker is not there: this file's job is the policy, not the front end.
UI_ROWS = [dict(rel(MBID["cd"], fmt="CD"), title="Homework"),
           dict(rel(MBID["vinyl"], fmt="Vinyl"), title="Homework"),
           dict(rel(MBID["digital"], fmt="Digital Media"), title="Homework"),
           dict(rel(MBID["bootleg"], status="Bootleg", date="1999-01-01"), title="Homework")]
UI_GROUP = {"id": MBID["rg-1"], "title": "Homework", "first_release_date": "1997-01-20",
            "primary_type": "Album", "secondary_types": []}
UI_CASES = {
    # the ordinary pick, a user override, a group nothing eligible exists for,
    # and a filter the group's own type is not part of
    "plain": rc.choice_payload(MBID["rg-1"], UI_GROUP, UI_ROWS, cfg()),
    "forced": rc.choice_payload(MBID["rg-1"], UI_GROUP, UI_ROWS, cfg(), prefer=MBID["vinyl"]),
    "refused": rc.choice_payload(MBID["rg-1"], UI_GROUP,
                                 [dict(rel(MBID["promo"], status="Promotion", tracks=1),
                                       title="Homework")], cfg()),
    "wrongkind": rc.choice_payload(MBID["rg-1"], UI_GROUP, UI_ROWS, cfg(),
                                   primary_type="single"),
}
assert UI_CASES["forced"]["chosen"]["release_mbid"] == MBID["vinyl"]
assert UI_CASES["refused"]["chosen"]["eligible"] is False
assert UI_CASES["wrongkind"]["chosen"] is None

_ui_note = "panel checker skipped (no node or tools/check_release_choice.mjs)"
_ui_checker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "check_release_choice.mjs")
if shutil.which("node") and os.path.exists(_ui_checker):
    _ui_dir = tempfile.mkdtemp(prefix="mlo-choice-ui-")
    try:
        _ui_paths = []
        for _name, _payload in UI_CASES.items():
            _p = os.path.join(_ui_dir, f"{_name}.json")
            with open(_p, "w", encoding="utf-8") as fh:
                json.dump(_payload, fh, ensure_ascii=False, indent=1)
            _ui_paths.append(_p)
        _run = subprocess.run(["node", _ui_checker] + _ui_paths,
                              capture_output=True, text=True, cwd=ROOT, timeout=600)
        if _run.returncode == 2:
            # The checker's own convention (tools/check_*.mjs): 2 = the
            # environment cannot run it (no web/node_modules — npm install),
            # 1 = a check failed. An environment gap is not drift, so it skips
            # loudly; only a failed check is the failure this pairing is for.
            _ui_note = ("panel checker SKIPPED: "
                        + (_run.stderr.strip().splitlines() or ["exit 2"])[-1])
        else:
            assert _run.returncode == 0, (_run.stdout[-2000:], _run.stderr[-2000:])
            _ui_note = _run.stdout.strip().splitlines()[-1]
    finally:
        shutil.rmtree(_ui_dir, ignore_errors=True)

print("ok  release-choice policy: status/medium/completeness/original/plain/country "
      "ranked by one module, deterministic, type-filtered, and used by the "
      "release-group page, group_targets, resolve_release, auto_import_targets, "
      "pick_releases, the artist watch and GET /api/mb/release-choice; UI: "
      + _ui_note)
