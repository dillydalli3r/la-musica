"""Auto-import release-choice policy (`integrations.pick_releases`).

Which MusicBrainz edition auto-import downloads is the one decision the user
never gets to see before it happens, so the rules are pinned here: a bootleg or
promotion must never win over an official pressing, an edition with no
RELEASECOUNTRY is not eligible at all, and the medium preference decides
between otherwise equal official editions.

Run: python tools/test_release_choice.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import integrations as intg  # noqa: E402


def rel(title, status="", country="", fmt="CD", date="2000-01-01", mbid=None):
    return {"id": mbid or title, "title": title, "status": status,
            "country": country, "formats": [fmt], "date": date}


CFG = {"auto_import_avoid_promo": True, "auto_import_require_country": True,
       "auto_import_medium_order": ["CD", "Digital Media", "Vinyl", "Cassette", "Other"]}


def pick(rows, cfg=None):
    return [r["title"] for r in intg.pick_releases(rows, cfg or CFG)]


# Official beats promo/bootleg/pseudo outright — a rip that is not the album
# must never be auto-imported while a real edition exists.
assert pick([rel("promo", status="Promotion", country="US"),
             rel("bootleg", status="Bootleg", country="US"),
             rel("album", status="Official", country="US")]) == ["album"]
# …and with nothing else on offer, the group is INELIGIBLE rather than a
# bootleg download (the job reports "no edition eligible").
assert pick([rel("promo", status="Promotion", country="US"),
             rel("bootleg", status="Bootleg", country="US")]) == []

# A release country is required: an official edition without one is skipped.
assert pick([rel("no-country", status="Official", date="1990-01-01"),
             rel("country", status="Official", country="GB", date="2020-01-01")]) == ["country"]
assert pick([rel("no-country", status="Official")]) == []
# The country requirement is a policy, not a hard-wired rule.
off = {"auto_import_avoid_promo": True, "auto_import_require_country": False,
       "auto_import_medium_order": ["CD"]}
assert pick([rel("no-country", status="Official")], off) == ["no-country"]

# Status ladder among the rest: plain > withdrawn/expired/cancelled > promo.
assert pick([rel("withdrawn", status="Withdrawn", country="US"),
             rel("plain", status="", country="US")]) == ["plain", "withdrawn"]
assert pick([rel("cancelled", status="Cancelled", country="US"),
             rel("expired", status="Expired", country="US"),
             rel("official", status="Official", country="US")])[0] == "official"

# Official editions of the same group: medium preference, then earliest date.
assert pick([rel("digital", status="Official", country="US", fmt="Digital Media", date="1990-01-01"),
             rel("cd", status="Official", country="US", fmt="CD", date="2010-01-01")]) == ["cd", "digital"]
assert pick([rel("later", status="Official", country="US", date="2010-01-01"),
             rel("earlier", status="Official", country="US", date="1994-03-01")]) == ["earlier", "later"]
# Same year, different precision: the edition that states the DAY wins over
# the one stating only the year — the album folder is named after this date,
# so a year-only edition would pin the folder to a year (a real library had
# exactly that: one 1997 US CD "1997" over the 1983-09-13 US CD).
assert pick([rel("year-only", status="Official", country="US", date="1983"),
             rel("full", status="Official", country="US", date="1983-09-13")]) == ["full", "year-only"]
assert pick([rel("ym", status="Official", country="US", date="1980-10"),
             rel("y", status="Official", country="US", date="1980")]) == ["ym", "y"]
# …but an earlier edition still beats a later, fuller one: the year is the
# primary term and only a TIE on the year prefers precision.
assert pick([rel("full-later", status="Official", country="US", date="1994-03-01"),
             rel("year-earlier", status="Official", country="US", date="1983")]) == \
    ["year-earlier", "full-later"]
# An edition with no date at all ranks after every dated one.
assert pick([rel("undated", status="Official", country="US", date=""),
             rel("year", status="Official", country="US", date="1990")]) == ["year", "undated"]
# An unknown format ranks after every configured one but still sorts by date.
assert pick([rel("weird", status="Official", country="US", fmt="Minidisc"),
             rel("cd", status="Official", country="US", fmt="CD")]) == ["cd", "weird"]

# "all" mode is every ELIGIBLE edition: the drops above apply there too.
rows = [rel("official", status="Official", country="US"),
        rel("bootleg", status="Bootleg", country="US"),
        rel("no-country", status="Official")]
assert pick(rows) == ["official"]

# A group id resolves to its best RELEASE through the same policy.
assert intg.pick_release([rel("bootleg", status="Bootleg", country="US"),
                          rel("album", status="Official", country="US")])["title"] == "album"
assert intg.pick_release([rel("promo", status="Promotion", country="US")]) is None

print("ok  release-choice policy: promo/bootleg dropped, country required, medium+date ordered")
