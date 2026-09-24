#!/usr/bin/env python3
"""Cover-pick PARITY: the dialog and the unattended import land the same image.

The owner's ask: an unattended import / add-to-library must land the SAME cover
the Cover Search dialog recommends as its BEST PICK — by the same rule, from the
same inputs, with the same reason stored. Both surfaces already ask
`mlo.cover_choice` (the ONE policy), but a policy only picks the same image if
it is handed the same ROWS: this test drives BOTH entry points over one
synthetic candidate set and compares what they chose, why, and what landed.

  * the DIALOG's path is the app's own route — `GET /api/cover/search` through
    `TestClient(server.main.app)`, which is what `CoverSearchModal` fetches
    (`web/src/lib/coverSearch.ts` sends no `limit`, so the route's default is
    the dialog's ask). The BEST PICK card renders `reasons[reasons.length - 1]`
    (`candidateReason`), which is the sentence compared below;
  * the IMPORT's path is the real chain step every unattended path runs —
    `server.imports.run_cover_step` — in both modes: staging the ranked
    candidates (`cover_review` on) and WRITING the winner (the shipped default,
    which is what an auto-import or an album added to the library gets).

Only the PROVIDER seams are stubbed (`_cov_stream`, `_advisory_json`,
`_probe_get`, `cov_catalog`, `release_lookup`, and the writer's download): the
finder runs for real, including its truncation of the COV stream at the ask
(`_cov_results`) and the release-group identity read. That is what makes the
comparison mean something — what this pins:

  * the two paths ask the finder the SAME question: the same COV request (the
    same artist, album, source ids and region), the same Cover Art Archive
    endpoints, and the same number of candidates. The album's tags state only a
    RELEASE id, so both must recover the release GROUP (one of them asking the
    group endpoint while the other cannot is exactly the divergence this
    catches: without the group the import would rank the album's own art and
    the dialog never could);
  * the same WINNER, the same deciding sentence, the same ranked rows with the
    same verdicts and reasons, and the same policy report. No winner here is
    the first row the provider listed, so a surface that took row 0 (or the
    first hit) instead of consulting the policy fails;
  * the cases the ranking has to get right, through BOTH paths: a row below the
    configured floor, a row nothing could be asked about (so nothing was ever
    measured — the floor covers it too), another release's cover (rejected,
    naming what contradicted), an oversized file, an upscaled thumbnail, a
    provider that stated its size as text (probed by nobody), a source the
    configured order does not name, one release's own sleeve vs the release
    GROUP's art, two sources at the same size (the configured SOURCE ORDER
    decides), the FORMAT tier, and a tie only the provider's own order breaks;
  * the truncation: one scenario's winner is the 40th row, so an import asking
    for a screenful of 12 would land a different image (the parity bug this
    test exists for);
  * the file the import wrote is the winner's own image, fetched from the
    winner's URL (`substitute=False`, so no other provider may answer), and the
    line the import reports carries the dialog's own deciding sentence.

Run: python tools/test_cover_parity.py
Exit 0 = pass, 2 = skip (no fastapi/httpx, so no TestClient).
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from fastapi.testclient import TestClient
except Exception as e:                                   # pragma: no cover
    print(f"SKIP: TestClient unavailable ({e})")
    raise SystemExit(2)

from PIL import Image                                    # noqa: E402

from mlo import audio as mlo_audio                        # noqa: E402
from mlo import cover_choice as cc                        # noqa: E402
from server import imports as imp                         # noqa: E402
from server import integrations as intg                   # noqa: E402
from server import main as mlo_main                       # noqa: E402

# --------------------------------------------------------------------------- #
# A scratch library, the shipped cover numbers, and the album's identity
# --------------------------------------------------------------------------- #
TMP = tempfile.mkdtemp(prefix="mlo-cover-parity-")
MUSIC = os.path.join(TMP, "music")
os.makedirs(os.path.join(MUSIC, ".mlo", "data"))

ARTIST = "Parity Artist"
ALBUM = "Parity Album"
TRACKS = 10
# The album's tags state its RELEASE but not its release group (a folder tagged
# by another tool): both paths have to resolve the group from the release id, or
# they are asking the finder about two different albums.
RELEASE_MBID = "11111111-1111-1111-1111-111111111111"
GROUP_MBID = "22222222-2222-2222-2222-222222222222"

CFG = {"music_folder": MUSIC,
       # the shipped cover numbers (the floor IS `cover_target_size` here)
       "cover_target_size": 1200, "cover_resize_enabled": True,
       "cover_force_exact_size": True, "grader_strict_square_threshold": 0.0,
       "cover_crop_threshold": 0.0, "cover_crop_enabled": True,
       "cover_enforce_square": True, "grade_check_cover_crop": True,
       "cover_jpeg_quality": 90,
       # the configured source order BOTH paths rank with: qobuz first, and
       # spotify is deliberately not named by it at all
       "cover_sources": ["qobuz", "deezer", "itunes", "tidal"],
       "cover_auto_fetch": True, "cover_review": False,
       "metadata_auto_fetch": False, "rym_links_auto": False}

mlo_main.load_config = lambda *a, **k: dict(CFG)
# The image probe's memo, and the catalogue COV answers /api/info with: both
# live under the app's data dir and are stubbed to this test's own space.
intg._data_cache_dir = lambda name: os.path.join(TMP, name)
CATALOG = {"sources": [{"id": i, "name": i, "enabled": True, "color": None,
                        "countries": ["us"]}
                       for i in ("qobuz", "deezer", "itunes", "tidal", "spotify",
                                 "musicbrainz")],
           "countries": ["us"], "active_source_limit": 9}
intg.cov_catalog = lambda timeout=15.0: copy.deepcopy(CATALOG)
# The release group is the only MusicBrainz read either path makes.
lookups = []
intg.release_lookup = lambda mbid: (lookups.append(str(mbid)) or
                                    {"release_group_id": GROUP_MBID,
                                     "id": str(mbid)})


# The album's tags come from a table (building real tagged audio would test
# mutagen instead of the parity): `server.imports` reads them through
# `mlo.audio.AudioFile`, exactly as it reads a real folder's.
_TAGS = {}


class FakeAudioFile:
    def __init__(self, path):
        self.path = path
        self.audio = object()          # not None → the tags are read
        self.tags = _TAGS.get(os.path.normpath(str(path)), {})

    def get_tag(self, name):
        return self.tags.get(name)


mlo_audio.AudioFile = FakeAudioFile


def album(name):
    """A fresh album folder: one track whose tags state the album, the release
    id and the track total the identity check reads."""
    path = os.path.join(MUSIC, "Artists", name)
    os.makedirs(path, exist_ok=True)
    track = os.path.join(path, "01 - Track.flac")
    open(track, "wb").close()
    _TAGS[os.path.normpath(track)] = {
        "ALBUMARTIST": ARTIST, "ALBUM": ALBUM, "TRACKTOTAL": str(TRACKS),
        "MUSICBRAINZ_ALBUMID": RELEASE_MBID}
    return path


# --------------------------------------------------------------------------- #
# The provider seams: COV's stream, the CAA's JSON, the image probe
# --------------------------------------------------------------------------- #
REL = {"title": ALBUM, "artist": ARTIST, "tracks": TRACKS, "url": "https://rel/"}
KARAOKE = {"title": "Karaoke Hits", "artist": ARTIST, "tracks": TRACKS,
           "url": "https://rel/karaoke"}


def cov_event(source, url, release=REL, **extra):
    """One COV `cover` event: the source's own statement (the image URLs and
    the release it matched), plus whatever else the site states about it."""
    event = {"type": "cover", "source": source, "bigCoverUrl": url,
             "smallCoverUrl": url.replace(".jpg", "-500.jpg"),
             "releaseInfo": release}
    event.update(extra)
    return event


def image(w, h, fmt="jpeg"):
    """Real image bytes of exactly w×h, so the probe reads a real size."""
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 90, 200)).save(buf, "JPEG" if fmt == "jpeg" else "PNG")
    return buf.getvalue()


def caa_image(scope, ident, size=1200):
    """One Cover Art Archive image record, in the archive's own shape: the
    full-size URL names the size it serves, and there is a thumbnail beside it
    (CAA states no dimensions — the probe is what measures it)."""
    big = f"{intg.CAA_BASE}/{scope}/{ident}/front-{size}.jpg"
    return {"front": True, "types": ["Front"], "image": big,
            "thumbnails": {"large": big.replace(f"-{size}.jpg", "-500.jpg"),
                           "small": big.replace(f"-{size}.jpg", "-250.jpg")}}


# The scenario the next finder call answers for: the COV events, the images the
# probe finds (a URL that is absent could not be asked, so nothing was
# measured), and the archive's own images for this album's two ids.
SCENARIO = {"name": "s1", "cov": [], "probe": {}, "group": [], "release": []}
cov_bodies = []
caa_reads = []


@contextlib.contextmanager
def fake_cov_stream(body, headers=None, timeout=60.0):
    """The COV request seam: records the question and answers the scenario's
    events — the real `_cov_results` does the truncating, at the caller's ask."""
    cov_bodies.append({"body": dict(body), "headers": dict(headers or {}),
                       "timeout": timeout})
    yield iter([json.dumps(e) for e in SCENARIO["cov"]])


def fake_advisory_json(url, params=None, headers=None, timeout=None, host=None):
    """The Cover Art Archive seam: one endpoint per id, and the scenario says
    what — if anything — each holds."""
    caa_reads.append(url)
    if "release-group/" in url:
        return {"images": copy.deepcopy(SCENARIO["group"])}
    if "/release/" in url:
        return {"images": copy.deepcopy(SCENARIO["release"])}
    return None


def fake_probe_get(url, nbytes=intg.COVER_PROBE_BYTES, timeout=10.0):
    """The ranged-GET seam: the bytes this URL answers with, or None when the
    host could not be asked at all (which is not the same as an empty answer)."""
    return SCENARIO["probe"].get(url)


intg._cov_stream = fake_cov_stream
intg._advisory_json = fake_advisory_json
intg._probe_get = fake_probe_get
# Host resolution is not the subject here (the SSRF guard has its own tests),
# and a scratch host name resolves nowhere.
intg._public_host = lambda host: bool(host)


def load_scenario(name):
    """Point the seams at one scenario and forget the probe/catalog memos."""
    scen = SCENARIOS[name]
    SCENARIO.update(name=name, cov=copy.deepcopy(scen["cov"]),
                    probe=dict(scen["probe"]), group=copy.deepcopy(scen["group"]),
                    release=copy.deepcopy(scen["release"]))
    intg._GENRE_CACHE.clear()
    return scen


# --------------------------------------------------------------------------- #
# The scenarios — the shapes the providers really return
# --------------------------------------------------------------------------- #
def s1():
    """The configured SOURCE ORDER decides, and the winner is listed LAST."""
    rows = [
        # bigger than the target, which is not better (size tier)
        ("itunes", "https://img.test/s1-a1400.jpg", REL, 1400, "jpeg"),
        # the same names, another release → REJECTED (identity)
        ("itunes", "https://img.test/s1-karaoke.jpg", KARAOKE, 1400, "jpeg"),
        # measures 1200 but its URL asks the CDN for 250 → upscaled (quality)
        ("deezer", "https://cdn.test/deezer/250x250-1234.jpg", REL, 1200, "jpeg"),
        # the same size and shape as the winner, from a source the order ranks
        # lower → the source tier decides against it
        ("deezer", "https://img.test/s1-deezer.jpg", REL, 1200, "jpeg"),
        # a source the configured order does not name at all
        ("spotify", "https://img.test/s1-spotify.jpg", REL, 1200, "jpeg"),
        # nothing could be asked about this image, so its size was never
        # measured → REJECTED (the floor covers an unmeasured row too)
        ("itunes", "https://img.test/s1-unmeasured.jpg", REL, None, None),
        # the same source as the winner, in PNG (the format tier)
        ("itunes", "https://img.test/s1-itunes.png", REL, 1200, "png"),
        # measured, and 300px short of the floor → REJECTED (floor)
        ("tidal", "https://img.test/s1-tidal-900.jpg", REL, 900, "jpeg"),
        # a provider that stated its size as TEXT and was never probed — and
        # THE WINNER
        ("qobuz", "https://img.test/s1-qobuz.jpg", REL, None, None),
    ]
    cov = [cov_event(src, url, rel) for src, url, rel, _, _ in rows]
    cov[8].update(width="1200", height="1200")     # the text size
    sleeve = caa_image("release", RELEASE_MBID)
    return {"cov": cov,
            "probe": {**{url: image(side, side, fmt)
                         for _, url, _, side, fmt in rows if side},
                      sleeve["image"]: image(1200, 1200)},
            "group": [],
            # one RELEASE's own sleeve: below a name-searched row (release tier)
            "release": [sleeve],
            "winner": "https://img.test/s1-qobuz.jpg", "winner_index": 8,
            "decides": "the configured source order"}


def s2():
    """The release tier decides: the release GROUP's art wins over both a
    name-searched row and one release's own sleeve."""
    return {"cov": [cov_event("qobuz", "https://img.test/s2-qobuz.jpg", REL)],
            "probe": {"https://img.test/s2-qobuz.jpg": image(1200, 1200),
                      f"{intg.CAA_BASE}/release-group/{GROUP_MBID}/front-1200.jpg":
                          image(1200, 1200),
                      f"{intg.CAA_BASE}/release/{RELEASE_MBID}/front-1200.jpg":
                          image(1200, 1200)},
            "group": [caa_image("release-group", GROUP_MBID)],
            "release": [caa_image("release", RELEASE_MBID)],
            # the group's image is the last row the finder appends
            "winner": f"{intg.CAA_BASE}/release-group/{GROUP_MBID}/front-1200.jpg",
            "winner_index": 2,
            "decides": "the album's own cover (the release group's art)"}


def s3():
    """The format tier decides (the library stores JPEG)."""
    return {"cov": [cov_event("qobuz", "https://img.test/s3-qobuz.png", REL),
                    cov_event("qobuz", "https://img.test/s3-qobuz.jpg", REL)],
            "probe": {"https://img.test/s3-qobuz.png": image(1200, 1200, "png"),
                      "https://img.test/s3-qobuz.jpg": image(1200, 1200, "jpeg")},
            "group": [], "release": [],
            "winner": "https://img.test/s3-qobuz.jpg", "winner_index": 1,
            "decides": "the image format"}


def s4():
    """Only the LAST tiebreak is left: two identical rows, and the provider's
    own order keeps the one it listed first."""
    return {"cov": [cov_event("itunes", "https://img.test/s4-a1400.jpg", REL),
                    cov_event("qobuz", "https://img.test/s4-qobuz-a.jpg", REL),
                    cov_event("qobuz", "https://img.test/s4-qobuz-b.jpg", REL)],
            "probe": {url: bytes_ for url, bytes_ in (
                ("https://img.test/s4-a1400.jpg", image(1400, 1400)),
                ("https://img.test/s4-qobuz-a.jpg", image(1200, 1200)),
                ("https://img.test/s4-qobuz-b.jpg", image(1200, 1200)))},
            "group": [], "release": [],
            "winner": "https://img.test/s4-qobuz-a.jpg", "winner_index": 1,
            "decides": "the provider's own order"}


def s5():
    """The finder TRUNCATES at the ask, and the policy's winner sits past the
    twelfth row — so an import asking for fewer candidates than the dialog
    would never see it. Every event states its size (COV's own lines can), and
    none of them is probed: the set is longer than the probe limit, and the
    ranking must not depend on what a probe happened to reach."""
    cov = [cov_event("deezer", f"https://img.test/s5-deezer-{i}.jpg", REL,
                     width=1200, height=1200) for i in range(39)]
    cov.append(cov_event("qobuz", "https://img.test/s5-qobuz.jpg", REL,
                         width=1200, height=1200))
    cov.extend(cov_event("deezer", f"https://img.test/s5-tail-{i}.jpg", REL,
                         width=1200, height=1200) for i in range(5))
    return {"cov": cov, "probe": {}, "group": [], "release": [],
            "winner": "https://img.test/s5-qobuz.jpg", "winner_index": 39,
            "decides": "the configured source order"}


SCENARIOS = {"s1": s1(), "s2": s2(), "s3": s3(), "s4": s4(), "s5": s5()}


# --------------------------------------------------------------------------- #
# The writer's own download seam
# --------------------------------------------------------------------------- #
fetched = []


def fake_url_bytes(url, artist="", substitute=True):
    """What the cover WRITE downloads: the candidate's own URL (`substitute`
    False — no other provider may answer in its place)."""
    fetched.append({"url": url, "artist": artist, "substitute": substitute})
    return image(1200, 1200, "png"), "image/png"


mlo_main._cover_url_bytes = fake_url_bytes

# --------------------------------------------------------------------------- #
# Both paths, one scenario at a time
# --------------------------------------------------------------------------- #
client = TestClient(mlo_main.app)
FOLDERS = {name: album(f"Parity/{name}") for name in SCENARIOS}
Q = {"artist": ARTIST, "album": ALBUM, "tracks": TRACKS,
     "release_mbid": RELEASE_MBID}


def run_dialog(name):
    """The dialog's path: the app's own route, with no `limit` sent (the modal
    sends none, so the route's default is the dialog's ask)."""
    load_scenario(name)
    cov_bodies.clear()
    caa_reads.clear()
    r = client.get("/api/cover/search", params=Q)
    assert r.status_code == 200, (name, r.status_code, r.text[:400])
    return r.json(), list(cov_bodies), list(caa_reads)


def run_step(name, review):
    """The import's path: the chain's own cover step."""
    load_scenario(name)
    cov_bodies.clear()
    caa_reads.clear()
    out = imp.run_cover_step(FOLDERS[name], dict(CFG, cover_review=review))
    return out, list(cov_bodies), list(caa_reads)


RESULT = {}
for name in sorted(SCENARIOS):
    scen = load_scenario(name)
    rows = scen["cov"]

    dialog, dialog_cov, dialog_caa = run_dialog(name)
    staged_out, staged_cov, staged_caa = run_step(name, True)
    staged = imp.staged_metadata(FOLDERS[name],
                                 dict(CFG, cover_review=True))["covers"]
    written_out, written_cov, written_caa = run_step(name, False)

    # -- the same QUESTION: the same COV request, the same CAA endpoints ----
    assert dialog_cov and len(dialog_cov) == len(staged_cov) == 1 == len(written_cov), \
        (name, dialog_cov, staged_cov, written_cov)
    assert dialog_cov[0]["body"] == staged_cov[0]["body"] == written_cov[0]["body"], \
        (name, dialog_cov[0]["body"], staged_cov[0]["body"], written_cov[0]["body"])
    assert dialog_cov[0]["body"]["artist"] == ARTIST and \
        dialog_cov[0]["body"]["album"] == ALBUM, (name, dialog_cov[0]["body"])
    # ...including the identity read both had to RECOVER from the release id
    # alone: the album's own art is asked for by the release GROUP's id
    group_url = f"{intg.CAA_BASE}/release-group/{GROUP_MBID}"
    release_url = f"{intg.CAA_BASE}/release/{RELEASE_MBID}"
    assert dialog_caa == staged_caa == written_caa == [group_url, release_url], \
        (name, dialog_caa, staged_caa, written_caa)

    # -- the same WINNER, and the same deciding sentence --------------------
    assert dialog["chosen"] is not None, (name, dialog["notes"])
    chosen = dialog["chosen"]
    assert chosen["big"] != rows[0]["bigCoverUrl"], \
        (name, "the comparison would not catch taking the row the provider "
               "listed first")
    assert chosen["big"] == scen["winner"], (name, chosen["big"])
    assert staged["chosen"] == chosen, (name, staged["chosen"], chosen)
    assert written_out["choice"] == chosen, (name, written_out["choice"], chosen)
    deciding = chosen["reasons"][-1]
    assert staged["chosen"]["reasons"][-1] == deciding, (name, staged["chosen"])
    assert written_out["choice"]["reasons"][-1] == deciding, (name, written_out)
    # ...the tier the scenario is built around really decided it
    assert scen["decides"] in deciding, (name, deciding)
    # ...and the line the import REPORTS carries the dialog's own sentence
    assert deciding in written_out["note"], (name, written_out["note"])

    # -- every ROW's verdict agrees (order, score, reasons) -----------------
    ranked = min(len(rows) + len(scen["group"]) + len(scen["release"]),
                 cc.CANDIDATE_LIMIT)
    assert staged["notes"] == dialog["notes"], (name, staged["notes"], dialog["notes"])
    assert staged["identity"] == dialog["identity"], (name, staged["identity"])
    assert staged["policy"] == dialog["policy"], (name, staged["policy"])
    assert len(staged["results"]) == len(dialog["results"]) == ranked, \
        (name, len(staged["results"]), len(dialog["results"]), ranked)
    for mine, theirs in zip(staged["results"], dialog["results"]):
        assert {k: v for k, v in mine.items() if k != "reasons"} == \
            {k: v for k, v in theirs.items() if k != "reasons"}, (name, mine, theirs)
        assert mine["reasons"] == theirs["reasons"], (name, mine, theirs)

    # -- the same file landed, and it is the winner's own image -------------
    assert fetched[-1]["url"] == chosen["big"], (name, fetched[-1], chosen)
    assert fetched[-1]["substitute"] is False, (name, fetched[-1])
    path = written_out["applied"]["cover"]
    assert os.path.isfile(path), (name, path)
    with Image.open(path) as img:
        assert img.size == (chosen["width"], chosen["height"]), (name, img.size)
    assert written_out["candidates"] == min(
        len(rows) + len(scen["group"]) + len(scen["release"]), cc.SEARCH_LIMIT), \
        (name, written_out["candidates"], len(rows))
    assert written_out["fetched"] is True and written_out["staged"] is False, \
        (name, written_out)
    assert staged_out["staged"] is True and staged_out["applied"] == {}, staged_out
    RESULT[name] = {"dialog": dialog, "staged": staged, "staged_out": staged_out,
                    "written_out": written_out}

# --------------------------------------------------------------------------- #
# Every case the ranking has to get right, read back through BOTH paths
# --------------------------------------------------------------------------- #
s1_pick, r1 = RESULT["s1"]["staged"], RESULT["s1"]["dialog"]


def row_of(payload, url):
    found = next((c for c in payload["results"] if c["big"] == url), None)
    assert found is not None, (url, [c["big"] for c in payload["results"]])
    return found


for payload, who in ((s1_pick, "import"), (r1, "dialog")):
    # a candidate BELOW the configured floor is rejected, with the floor named,
    # and is still listed (a user may apply it by hand)
    short = row_of(payload, "https://img.test/s1-tidal-900.jpg")
    assert "900×900 is below the minimum 1200×1200" in short["rejected"], (who, short)
    # so is one whose size was NEVER measured — the floor covers it too
    unmeasured = row_of(payload, "https://img.test/s1-unmeasured.jpg")
    assert "never measured" in unmeasured["rejected"] and \
        "1200×1200 minimum" in unmeasured["rejected"], (who, unmeasured)
    # another release's cover is rejected, naming what contradicted
    karaoke = row_of(payload, "https://img.test/s1-karaoke.jpg")
    assert "Karaoke Hits" in karaoke["rejected"] and \
        "not this album's cover" in karaoke["rejected"], (who, karaoke)
    # the oversized file and the upscaled thumbnail both ranked below the winner
    winner = row_of(payload, "https://img.test/s1-qobuz.jpg")
    assert row_of(payload, "https://img.test/s1-a1400.jpg")["score"] < \
        winner["score"], who
    assert "upscaled" in " ".join(
        row_of(payload, "https://cdn.test/deezer/250x250-1234.jpg")["reasons"]), who
    # the size a provider stated as text was read as the size it is, and the
    # container nobody measured says so
    assert winner["width"] == 1200 and winner["height"] == 1200, (who, winner)
    assert "container was not read" in " ".join(winner["reasons"]), (who, winner)
    # one release's OWN sleeve ranks below a name-searched row (release tier)
    assert "one release's own front cover" in " ".join(row_of(
        payload, f"{intg.CAA_BASE}/release/{RELEASE_MBID}/front-1200.jpg"
    )["reasons"]), who
    # a source the configured order does not name ranks after every named one
    assert "not in the configured cover source order" in " ".join(
        row_of(payload, "https://img.test/s1-spotify.jpg")["reasons"]), who

# the tie only the provider's own order breaks: the loser says so, in both paths
s4_pick, r4 = RESULT["s4"]["staged"], RESULT["s4"]["dialog"]
for payload, who in ((s4_pick, "import"), (r4, "dialog")):
    assert payload["chosen"]["big"] == "https://img.test/s4-qobuz-a.jpg", (who, payload)
    loser = row_of(payload, "https://img.test/s4-qobuz-b.jpg")
    assert loser["rejected"] is None and \
        "on the provider's own order" in loser["reasons"][-1], (who, loser)

# the truncation: the winner is the 40th row, so BOTH paths had to ask for the
# same number of candidates to see it — an import asking for 12 would land the
# 1200×1200 deezer row the finder listed first instead
s5_pick, r5 = RESULT["s5"]["staged"], RESULT["s5"]["dialog"]
assert r5["chosen"]["big"] == s5_pick["chosen"]["big"] == \
    "https://img.test/s5-qobuz.jpg", (r5["chosen"]["big"], s5_pick["chosen"]["big"])
# ...and neither path was ever offered the 1200×1200 deezer row it would have
# landed instead: both ranked exactly the same 40 rows out of the 45 streamed
assert r5["candidate_count"] == RESULT["s5"]["staged_out"]["candidates"] == \
    cc.SEARCH_LIMIT, (r5["candidate_count"], RESULT["s5"]["staged_out"])
assert len(r5["results"]) == len(s5_pick["results"]) == cc.CANDIDATE_LIMIT, \
    (len(r5["results"]), len(s5_pick["results"]))
assert r5["chosen"]["reasons"][-1] == s5_pick["chosen"]["reasons"][-1]

print("cover-pick parity (dialog route + import cover step): all checks passed")
