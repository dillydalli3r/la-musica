#!/usr/bin/env python3
"""The lyrics chain's two fallbacks: PLAIN text, and the ALIAS pass.

Offline: every provider, the MusicBrainz reads and the "was this track marked
instrumental" check are stubbed, and the audio files are real ones (tiny FLACs)
so the write path under test is the shipped one.

Pinned here:

  * a synced answer still wins — a plain one is remembered and walked PAST, so
    a later provider's timestamps are taken even when an earlier provider had
    the text without them,
  * when NO source states timestamps the plain answer IS the answer: written in
    the configured `lyrics_format`, `kind == "plain"`, status "ok" — and the
    track is never reported as having no lyrics (`_mark_lyrics_absent` is not
    reached, so no INSTRUMENTAL=1 for a track whose words a source had),
  * with nothing from anywhere the old not-found behaviour is untouched,
    `_mark_lyrics_absent` included,
  * the alias pass: the second walk of the chain, only after a first pass that
    found nothing at all, only while `lyrics_search_aliases` is on, and it says
    which name and entity it found the track under,
  * `宇多田ヒカル`/`Hikari` ↔ `Hikaru Utada`/`光` in BOTH directions, with the CJK
    left intact by the normalising the chain matches with,
  * `server.integrations.search_aliases`: the ordered, de-duplicated name list
    the pass feeds on (locale first, then a Latin reading, `search hint`
    aliases and the stored name itself dropped),
  * the SAME fallback in the two other lookups that miss a stored name — the
    advisory route (`resolve_advisory_route`, its name-based sources re-asked
    under the alias names only after every source stated nothing for the
    stored ones) and the instrumental detector's LRCLIB lookup — original
    names first, and NO MusicBrainz request at all when the file carries no
    ids,
  * publishing: a track with aliases is submitted under the original name pair
    AND each alias pair, with independent outcomes (a 409 under one name skips
    only that name), and `lyrics_search_aliases` off leaves the one original
    pair.

Run:  python tools/test_lyrics_aliases.py
"""
import contextlib
import io
import math
import os
import struct
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import lyrics_fetch as lf  # noqa: E402
from mlo import lyrics_providers as lp  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from server import integrations as si  # noqa: E402
from server import instrumental as inst  # noqa: E402

failures = []
checks = 0


def check(cond, label):
    global checks
    checks += 1
    if not cond:
        failures.append(label)
        print("FAIL:", label)
    return bool(cond)


class Patch:
    """Swap module attributes for a block and always restore them."""

    def __init__(self, obj, **attrs):
        self.obj, self.attrs, self.old = obj, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.old[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.obj, k, v)
        return False


def stub(answers, calls, name="stub"):
    """A provider keyed on the QUERY it is asked for.

    *answers* maps ``(artist, title)`` to the hit that provider would return
    (usually ``None``: that source does not know the track under that name) and
    *calls* records every query, which is how "one pass, never two" is proven.
    """
    def provider(artist, title, album=None, duration=None, cfg=None,
                 youtube_id=None):
        calls.append((name, artist, title))
        for (want_artist, want_title), hit in answers.items():
            if want_artist == artist and want_title == title:
                return hit
        return None
    return provider


SYNCED_LRC = ("[00:19.92]listen close and don't be stoned\n"
              "[00:22.40]i'll be here in the morning\n")
PLAIN_TEXT = "listen close and dont be stoned\ni'll be here in the morning"
PLAIN_LYRICS = PLAIN_TEXT + "\n"

SYNCED_HIT = lp._hit(SYNCED_LRC, lp._lrc_to_plain(SYNCED_LRC),
                     "Slowdive", "Alison", "Souvlaki", None)
PLAIN_HIT = lp._hit(None, PLAIN_LYRICS, "Slowdive", "Alison", "Souvlaki", None)
# Exact title, artist the source does not know: 0.65 + 0.35*0.4 = 0.79 — a
# candidate under the loose floor (0.6), refused by the automatic one (0.85).
WEAK_PLAIN_HIT = lp._hit(None, "someone else's words", None, "Alison", None,
                         None)
# 53s away from the track's own duration: killed outright, not merely weak.
FAR_OFF_HIT = lp._hit(None, "words from another recording", "Slowdive",
                      "Alison", None, 283.0)

# The Japanese/Latin pair the alias pass exists for: the SAME track, tagged
# either in Japanese or in Latin, with MusicBrainz stating the other name.
JP_ARTIST, JP_TITLE, JP_ALBUM = "宇多田ヒカル", "光", "ファーストラブ"
EN_ARTIST, EN_TITLE, EN_ALBUM = "Hikaru Utada", "Hikari", "First Love"
JP_SYNCED_HIT = lp._hit(SYNCED_LRC, lp._lrc_to_plain(SYNCED_LRC),
                        JP_ARTIST, JP_TITLE, JP_ALBUM, None)


# --------------------------------------------------------------------------- #
# 1. Synced first, plain as the fallback — the chain's own rule
# --------------------------------------------------------------------------- #
calls = []
with Patch(lp, _PROVIDERS={"lrclib": stub({("Slowdive", "Alison"): PLAIN_HIT},
                                          calls)}):
    hit = lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Slowdive", "Alison",
                          "Souvlaki", 230.0)
check(hit is not None,
      "a plain-only answer must not come back as 'no lyrics at all'")
check(hit and hit.get("kind") == "plain", f"kind must say plain: {hit}")
check(hit and hit["provider"] == "lrclib" and hit["provider_label"] == "LRCLIB",
      f"the plain fallback is filled in like any hit: {hit}")
check(hit and hit["plain"] == PLAIN_LYRICS.strip(), hit)
check(hit and hit["synced"] is None, hit)
check(hit and hit["score"] >= lp._MIN_SCORE, f"the score floor applies: {hit}")
check(hit and "alias_pass" not in hit, "the first pass is not an alias pass")
check(len(calls) == 1, f"one provider, one query: {calls}")

# a later provider's synced answer still wins, even though the first provider
# had usable (untimed) text
calls = []
with Patch(lp, _PROVIDERS={"lrclib": stub({("Slowdive", "Alison"): PLAIN_HIT},
                                          calls, name="lrclib"),
                           "kugou": stub({("Slowdive", "Alison"): SYNCED_HIT},
                                         calls, name="kugou")}):
    hit = lp.fetch_lyrics({"lyrics_sources": ["lrclib", "kugou"]},
                          "Slowdive", "Alison", "Souvlaki", 230.0)
check(hit and hit.get("kind") == "synced" and hit["provider"] == "kugou",
      f"synced must win over the plain fallback: {hit}")
check(hit and hit["synced"] == SYNCED_LRC.strip(), hit)
check([c[0] for c in calls] == ["lrclib", "kugou"],
      f"the plain answer is walked past, not returned: {calls}")

# two plain answers of the same score: the FIRST provider's is the one kept
calls = []
second_plain = lp._hit(None, "another copy of the words", "Slowdive", "Alison",
                       "Souvlaki", None)
with Patch(lp, _PROVIDERS={"lrclib": stub({("Slowdive", "Alison"): PLAIN_HIT},
                                          calls, name="lrclib"),
                           "kugou": stub({("Slowdive", "Alison"): second_plain},
                                         calls, name="kugou")}):
    hit = lp.fetch_lyrics({"lyrics_sources": ["lrclib", "kugou"]},
                          "Slowdive", "Alison", "Souvlaki", 230.0)
check(hit and hit["provider"] == "lrclib" and hit["plain"] == PLAIN_LYRICS.strip(),
      f"a tie keeps the first provider's answer: {hit}")

# a weak plain candidate is refused exactly like a weak synced one
calls = []
with Patch(lp, _PROVIDERS={"lrclib": stub({("Slowdive", "Alison"): WEAK_PLAIN_HIT},
                                          calls)}):
    check(lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Slowdive", "Alison",
                          "Souvlaki", 230.0, min_score=0.85) is None,
          "min_score must gate the plain fallback too")
    looser = lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Slowdive", "Alison",
                             "Souvlaki", 230.0)
    check(looser is not None and looser["kind"] == "plain",
          "the same answer under the loose search floor is a candidate")
# a duration 50s off is not a weak hit, it is another recording
with Patch(lp, _PROVIDERS={"lrclib": stub({("Slowdive", "Alison"): FAR_OFF_HIT},
                                          calls)}):
    check(lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Slowdive", "Alison",
                          "Souvlaki", 230.0) is None,
          "a plain hit from a different-length recording is refused outright")

# nothing at all still returns None
with Patch(lp, _PROVIDERS={"lrclib": stub({}, [])}):
    check(lp.fetch_lyrics({"lyrics_sources": ["lrclib"]}, "Slowdive", "Alison",
                          "Souvlaki", 230.0) is None,
          "no answer from anywhere is still None")


# --------------------------------------------------------------------------- #
# 2. What `fetch_one` writes: the plain text, honestly labelled
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in os.listdir(_deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break

TMP = tempfile.mkdtemp(prefix="mlo_lyrics_alias_test_")


def make_wav(path, seconds=1, freq=440):
    rate = 44100
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        chunks = []
        for i in range(int(rate * seconds)):
            v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
            chunks.append(struct.pack("<hh", v, v))
        w.writeframes(b"".join(chunks))


def make_flac(name, tags=None):
    from mutagen.flac import FLAC as MutFLAC

    path = os.path.join(TMP, name)
    wav = path + ".wav"
    make_wav(wav)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)
    if tags:
        f = MutFLAC(path)
        for key, value in tags.items():
            f[key] = value
        f.save()
    return path


CFG = {"music_folder": TMP, "lyrics_format": "EMBEDDED",
       "lyrics_sources": ["lrclib"]}

if not check(FLAC_EXE is not None, "flac.exe must be vendored for these tests"):
    for f in failures:
        print("FAIL:", f)
    raise SystemExit(1)

# (a) synced provider answers empty + plain provider answers -> written
path = make_flac("plain-embedded.flac",
                 {"ARTIST": "Slowdive", "TITLE": "Alison", "ALBUM": "Souvlaki"})
marked = []
with Patch(lf, _mark_lyrics_absent=lambda p, c, r: marked.append(p)), \
     Patch(lp, _PROVIDERS={"lrclib": stub({("Slowdive", "Alison"): PLAIN_HIT},
                                          [])}):
    res = lf.fetch_one(path, CFG)
check(res["status"] == "ok", f"a written plain fallback is status ok: {res}")
check(res.get("kind") == "plain", f"the result states the KIND: {res}")
check(res["synced"] is False, f"and `synced` stays truthful: {res}")
check(res["provider"] == "lrclib" and res["provider_label"] == "LRCLIB", res)
check(marked == [], "a plain fallback must never be reported as 'no lyrics'")
check(res["reason"] and "plain" in res["reason"],
      f"the reason says why the lyric is untimed: {res.get('reason')!r}")
check(AudioFile(path).get_lyrics() == PLAIN_TEXT,
      f"the plain text is written as configured: {AudioFile(path).get_lyrics()!r}")
check(AudioFile(path).get_tag("INSTRUMENTAL") in (None, "", "0"),
      "the track is NOT marked instrumental when a source had the words")

# ... and the same in LRC mode: the sidecar carries the text, no stamps invented
path_lrc = make_flac("plain-lrc.flac",
                     {"ARTIST": "Slowdive", "TITLE": "Alison"})
with Patch(lf, _mark_lyrics_absent=lambda p, c, r: None), \
     Patch(lp, _PROVIDERS={"lrclib": stub({("Slowdive", "Alison"): PLAIN_HIT},
                                          [])}):
    res = lf.fetch_one(path_lrc, dict(CFG, lyrics_format="LRC"))
sidecar = os.path.splitext(path_lrc)[0] + ".lrc"
check(res["status"] == "ok" and res.get("kind") == "plain", res)
check(res["wrote"]["lrc"] == sidecar and os.path.isfile(sidecar), res)
with open(sidecar, "r", encoding="utf-8") as fh:
    written = fh.read()
check(written.strip() == PLAIN_TEXT, f"the sidecar holds the plain text: {written!r}")
check("[00:" not in written, "no timestamp is invented for an untimed answer")

# (c) no provider answers at all -> the old not-found behaviour, marking included
path_none = make_flac("nothing.flac", {"ARTIST": "Slowdive", "TITLE": "Alison"})
with Patch(inst, detect_instrumental=lambda paths, cfg: {}), \
     Patch(lp, _PROVIDERS={"lrclib": stub({}, [])}):
    res = lf.fetch_one(path_none, CFG)
check(res["status"] == "skipped", f"not-found is still a skip: {res}")
check(res["reason"] == "no lyrics found — marked INSTRUMENTAL", res)
check(res.get("marked_instrumental") is True, res)
check(AudioFile(path_none).get_tag("INSTRUMENTAL") == "1",
      "the app's own answer to an empty search is untouched")


# --------------------------------------------------------------------------- #
# 3. The alias pass
# --------------------------------------------------------------------------- #
JP_TAGS = {"ARTIST": JP_ARTIST, "TITLE": JP_TITLE, "ALBUM": JP_ALBUM,
           "MUSICBRAINZ_ARTISTID": "art-1", "MUSICBRAINZ_TRACKID": "rec-1",
           "MUSICBRAINZ_RELEASEGROUPID": "rg-1"}
EN_TAGS = {"ARTIST": EN_ARTIST, "TITLE": EN_TITLE, "ALBUM": EN_ALBUM,
           "MUSICBRAINZ_ARTISTID": "art-2", "MUSICBRAINZ_TRACKID": "rec-2",
           "MUSICBRAINZ_RELEASEGROUPID": "rg-2"}

# The MusicBrainz answers: the Japanese entity's other-language names, and the
# Latin entity's Japanese ones (the same relation read from both sides).
MB = {
    "artist/art-1": {"aliases": [
        {"name": EN_ARTIST, "locale": "en", "primary": True},
        {"name": "Utada Hikaru", "locale": "en-Latn"},
        {"name": "宇多田ヒカル", "locale": "ja"},          # the stored name itself
        {"name": "Utada", "locale": "", "type": "Search hint"}]},
    "recording/rec-1": {"aliases": [
        {"name": EN_TITLE, "locale": "en-Latn", "primary": True},
        {"name": "Hikari (English Version)", "locale": ""}]},
    "release-group/rg-1": {"aliases": [{"name": EN_ALBUM, "locale": "en"}]},
    "artist/art-2": {"aliases": [
        {"name": JP_ARTIST, "locale": "ja", "primary": True}]},
    "recording/rec-2": {"aliases": [{"name": JP_TITLE, "locale": "ja"}]},
    "release-group/rg-2": {"aliases": [{"name": JP_ALBUM, "locale": "ja"}]},
    # The NAME searches the no-MBID path uses (the manual search box): one
    # exact-name result each, so the entity is unambiguous. The payload keys
    # mirror MusicBrainz's own — artists state `name`, recordings and
    # release-groups state `title`.
    "artist": {"artists": [{"id": "art-1", "name": JP_ARTIST}]},
    "recording": {"recordings": [{"id": "rec-1", "title": JP_TITLE}]},
    "release-group": {"release-groups": [{"id": "rg-1", "title": JP_ALBUM}]},
}
mb_calls = []


def fake_mb(endpoint, params=None, timeout=30.0, retries=5):
    mb_calls.append(endpoint)
    return MB.get(endpoint)


# (e) the stored (Japanese) name finds nothing; the alias does
path_alias = make_flac("alias-hit.flac", dict(JP_TAGS))
EN_HIT = lp._hit(SYNCED_LRC, lp._lrc_to_plain(SYNCED_LRC),
                 EN_ARTIST, EN_TITLE, EN_ALBUM, None)
answers = {(EN_ARTIST, EN_TITLE): EN_HIT,
           (JP_ARTIST, JP_TITLE): None}
calls = []
with Patch(inst, detect_instrumental=lambda paths, cfg: {}), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub(answers, calls)}):
    res = lf.fetch_one(path_alias, CFG)
check(res["status"] == "ok", f"the alias pass must produce a written lyric: {res}")
check(res["provider"] == "lrclib", res)
check(res.get("kind") == "synced", res)
check(res.get("alias_pass", {}).get("used") is True,
      f"the result must say the alias pass ran: {res.get('alias_pass')}")
pass_info = res.get("alias_pass") or {}
check(EN_ARTIST in str(pass_info.get("query", ""))
      and EN_TITLE in str(pass_info.get("query", "")),
      f"and which name produced the hit: {pass_info}")
check(pass_info.get("entity"), f"and which entity: {pass_info}")
check(calls[0] == ("stub", JP_ARTIST, JP_TITLE),
      f"the ordinary names are tried first: {calls}")
check((EN_ARTIST, EN_TITLE) in [c[1:] for c in calls],
      f"the alias names are tried after them: {calls}")
check(AudioFile(path_alias).get_lyrics() == SYNCED_LRC.strip(),
      f"the alias-pass lyric is written: {AudioFile(path_alias).get_lyrics()!r}")

# the RUN SUMMARY says the alias pass hit (and which kind was written).
# A FRESH track: the one above already holds the lyric it fetched, and script
# 13 fills only what is missing (force included) — a second run over it is
# booked as skipped, which is the behaviour `check_lyrics_fetch_never_replaces`
# in tools/test_script_optimizations.py pins.
path_alias_run = make_flac("alias-hit-run.flac", dict(JP_TAGS))
with Patch(inst, detect_instrumental=lambda paths, cfg: {}), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub(answers, [])}):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        stats = lf.run_fetch_lyrics(dict(CFG, targets=[path_alias_run]))
out = buf.getvalue()
check(stats["alias_count"] == 1, f"the run counts the alias hit: {stats}")
check(stats["by_kind"] == {"synced": 1, "plain": 0}, f"kind split: {stats['by_kind']}")
check("alias pass hit: 1" in out, f"the summary says so: {out!r}")
check("synced: 1" in out, f"and how many of the writes were synced: {out!r}")

# (f) `lyrics_search_aliases` false -> exactly one pass, and no MB lookups
path_off = make_flac("alias-off.flac", dict(JP_TAGS))
calls, mb_calls = [], []
with Patch(inst, detect_instrumental=lambda paths, cfg: {}), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub({}, calls)}):
    res = lf.fetch_one(path_off, dict(CFG, lyrics_search_aliases=False))
check(res["status"] == "skipped", f"nothing found: {res}")
check(len(calls) == 1, f"exactly one round of provider queries: {calls}")
check(mb_calls == [], f"and not even a MusicBrainz lookup: {mb_calls}")
check("alias_pass" not in res, res)

# (g) the ordinary name hits -> exactly one round, alias pass never fires
path_hit = make_flac("ordinary-hit.flac", dict(JP_TAGS))
calls, mb_calls = [], []
with Patch(inst, detect_instrumental=lambda paths, cfg: {}), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub({(JP_ARTIST, JP_TITLE): JP_SYNCED_HIT},
                                          calls, name="lrclib")}):
    res = lf.fetch_one(path_hit, CFG)
check(res["status"] == "ok" and res["provider"] == "lrclib", res)
check(len(calls) == 1, f"a hit costs ONE query: {calls}")
check(mb_calls == [], f"and no MusicBrainz round trip: {mb_calls}")
check("alias_pass" not in res, res)

# (h) the same relation read the OTHER way: Latin tags, Japanese aliases.
# The provider is keyed on the alias name, and the CJK must arrive intact.
path_rev = make_flac("alias-reverse.flac", dict(EN_TAGS))
rev_hit = lp._hit(SYNCED_LRC, lp._lrc_to_plain(SYNCED_LRC),
                  JP_ARTIST, JP_TITLE, JP_ALBUM, None)
calls = []
with Patch(inst, detect_instrumental=lambda paths, cfg: {}), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub({(JP_ARTIST, JP_TITLE): rev_hit},
                                          calls)}):
    res = lf.fetch_one(path_rev, CFG)
check(res["status"] == "ok", f"Latin tags, Japanese source name: {res}")
info = res.get("alias_pass") or {}
rev_query = str(info.get("query", ""))
check(JP_ARTIST in rev_query and JP_TITLE in rev_query,
      f"the CJK alias names are what found it: {info}")
check(AudioFile(path_rev).get_lyrics() == SYNCED_LRC.strip(), res)
# nothing in the matching path folds CJK away
check(lp._norm(JP_ARTIST) == JP_ARTIST, lp._norm(JP_ARTIST))
check(lp._norm(JP_TITLE) == JP_TITLE, lp._norm(JP_TITLE))
check(lp._name_score(JP_TITLE, JP_TITLE) == 1.0, "a CJK title matches itself")
check(lp._variant_guard(JP_TITLE, JP_TITLE) is True,
      "a CJK title is not read as a variant")

# the alias pass still respects the chain's acceptance rules: a weak alias hit
# is not written
path_weak = make_flac("alias-weak.flac", dict(JP_TAGS))
with Patch(inst, detect_instrumental=lambda paths, cfg: {}), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub(
         {(EN_ARTIST, EN_TITLE): lp._hit(None, "someone else's words",
                                         "Somebody Else", "Different Song",
                                         None, None),
          (JP_ARTIST, JP_TITLE): None}, [])}):
    res = lf.fetch_one(path_weak, CFG)
check(res["status"] == "skipped", f"a weak alias-pass match is refused: {res}")

# the RUN SUMMARY shows the split: which writes are synced and which are plain
path_s = make_flac("summary-synced.flac",
                   {"ARTIST": "Slowdive", "TITLE": "Alison"})
path_p = make_flac("summary-plain.flac",
                   {"ARTIST": "Slowdive", "TITLE": "Machine Gun"})
plain2 = lp._hit(None, PLAIN_LYRICS, "Slowdive", "Machine Gun", None, None)
with Patch(lp, _PROVIDERS={
        "lrclib": stub({("Slowdive", "Alison"): SYNCED_HIT}, [], name="lrclib"),
        "netease": stub({("Slowdive", "Machine Gun"): plain2}, [],
                        name="netease")}):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        stats = lf.run_fetch_lyrics(dict(CFG, targets=[path_s, path_p],
                                         lyrics_sources=["lrclib", "netease"]))
out = buf.getvalue()
check(stats["by_kind"] == {"synced": 1, "plain": 1},
      f"the run counts both kinds: {stats['by_kind']}")
check(stats["by_provider"] == {"lrclib": 1, "netease": 1},
      f"the provider counts keep their shape: {stats['by_provider']}")
check("lyrics fetched: 2 · skipped: 0 · failed: 0 · synced: 1 · plain: 1" in out,
      f"the summary line is kind-aware: {out!r}")
check("LRCLIB: 1 synced" in out and "NetEase: 1 plain" in out,
      f"and so is each provider's count: {out!r}")
check("alias pass hit" not in out, f"no alias pass ran here: {out!r}")


# --------------------------------------------------------------------------- #
# 4. `server.integrations.search_aliases`: the ordered name list
# --------------------------------------------------------------------------- #
ROWS = {"artist/art-1": {"aliases": [
    {"name": "Hikaru Utada", "locale": "en", "primary": True},
    {"name": "hikaru  utada", "locale": "en"},
    {"name": JP_ARTIST, "locale": "ja"},
    {"name": "Utada", "locale": "", "type": "Search hint"},
    {"name": "Utada Hikaru", "locale": "en-Latn"},
]}}
mb_calls = []
with Patch(si, mb_get_cached=lambda e, p=None, **k: (mb_calls.append(e),
                                                     ROWS.get(e))[1]):
    got = si.search_aliases("artist", "art-1", {}, JP_ARTIST)
check(got[0] == "Hikaru Utada", f"a Latin reading leads for a CJK name: {got}")
check("Utada Hikaru" in got, f"the rest of the readings follow: {got}")
check("Utada" not in got, f"a `search hint` alias is never a name to show: {got}")
check(JP_ARTIST not in got, f"the stored name is not its own alias: {got}")
check(len(got) == len(set(n.casefold() for n in got)),
      f"no case/space duplicate: {got}")
check(mb_calls == ["artist/art-1"], f"one MBID lookup, no search: {mb_calls}")
# the locale the reader asked for leads
with Patch(si, mb_get_cached=lambda e, p=None, **k: ROWS.get(e)):
    got_ja = si.search_aliases("artist", "art-1", {"locale": "ja"}, JP_ARTIST)
check(got_ja and got_ja[0] == "Hikaru Utada",
      f"an alias equal to the stored name is skipped, the next one leads: {got_ja}")

# the reverse direction: a Latin name whose aliases are Japanese
REV_ROWS = {"recording/rec-1": {"aliases": [
    {"name": "traveling", "locale": "", "primary": True},
    {"name": JP_TITLE, "locale": "ja"}]},
    # a recording SEARCH result states its name under `title`, not `name`
    "recording": {"recordings": [{"id": "rec-9", "title": "traveling"},
                                 {"id": "rec-8", "title": "traveling (Live)"}]},
    "recording/rec-9": {"aliases": [{"name": JP_TITLE, "locale": "ja"}]}}
with Patch(si, mb_get_cached=lambda e, p=None, **k: REV_ROWS.get(e)):
    got = si.search_aliases("recording", "rec-1", {}, "traveling")
check(got and JP_TITLE in got, f"a CJK alias is reachable from a Latin name: {got}")

# no MBID: the EXACT-name result supplies the aliases, never the fuzzy first one
with Patch(si, mb_get_cached=lambda e, p=None, **k: REV_ROWS.get(e)):
    got = si.search_aliases("recording", "", {}, "traveling")
check(got == [JP_TITLE], f"the exact-name entity's aliases, in order: {got}")
# ...and two entities with that exact name are no answer at all: picking one
# would search the providers with a stranger's names
AMBIGUOUS = dict(REV_ROWS, **{"recording": {"recordings": [
    {"id": "rec-9", "title": "traveling"}, {"id": "rec-7", "title": "traveling"}]}})
with Patch(si, mb_get_cached=lambda e, p=None, **k: AMBIGUOUS.get(e)):
    check(si.search_aliases("recording", "", {}, "traveling") == [],
          "an ambiguous name must not pick an entity")

# unknown entity, no MBID and no name: no lookup at all
mb_calls = []


def recording_mb(endpoint, params=None, **k):
    mb_calls.append(endpoint)
    return None


with Patch(si, mb_get_cached=recording_mb):
    check(si.search_aliases("label", "x", {}, "y") == [],
          "an entity with no alias lookup answers nothing")
    check(si.search_aliases("artist", "", {}, "") == [],
          "no MBID and no name is not a search")
    check(mb_calls == [], f"and neither reaches the network: {mb_calls}")


def boom(*a, **k):
    raise RuntimeError("musicbrainz is busy")


with Patch(si, mb_get_cached=boom):
    check(si.search_aliases("artist", "art-1", {}, JP_ARTIST) == [],
          "a MusicBrainz failure is no aliases, never an error")

# the chain's own view of the tags: which id, which name
tag_values = {"MUSICBRAINZ_ARTISTID": "art-1", "MUSICBRAINZ_TRACKID": "rec-1",
              "MUSICBRAINZ_RELEASEGROUPID": "rg-1"}


def get_tag(name):
    return tag_values.get(name)


with Patch(si, mb_get_cached=lambda e, p=None, **k: MB.get(e)):
    built = lf._search_aliases(get_tag, CFG, JP_ARTIST, JP_TITLE, JP_ALBUM)
check(built.get("artist") == [EN_ARTIST, "Utada Hikaru"], built)
check(built.get("title") == [EN_TITLE, "Hikari (English Version)"], built)
check(built.get("album") == [EN_ALBUM], built)
# the album-artist id is the fallback when the track artist has none, and an
# entity with NO id is left out — the batch path never searches by name
mb_calls = []
with Patch(si, mb_get_cached=lambda e, p=None, **k: mb_calls.append(e) or MB.get(e)):
    built = lf._search_aliases(
        lambda n: {"MUSICBRAINZ_ALBUMARTISTID": "art-1"}.get(n), CFG,
        JP_ARTIST, JP_TITLE, JP_ALBUM)
check(built == {"artist": [EN_ARTIST, "Utada Hikaru"]},
      f"only the tagged entity is asked: {built}")
check(mb_calls == ["artist/art-1"],
      f"and it costs one lookup, not a search per entity: {mb_calls}")
# a slot with no NAME has nothing to substitute either
with Patch(si, mb_get_cached=lambda e, p=None, **k: MB.get(e)):
    built = lf._search_aliases(get_tag, CFG, JP_ARTIST, "", "")
check(built.get("artist") and set(built) == {"artist"},
      f"an empty name has nothing to substitute: {built}")
# the setting is the switch, and it is checked before any lookup happens
mb_calls = []
with Patch(si, mb_get_cached=lambda e, p=None, **k: mb_calls.append(e)):
    check(lf._search_aliases(get_tag, {"lyrics_search_aliases": False},
                             JP_ARTIST, JP_TITLE, JP_ALBUM) == {},
          "the alias pass is off when the setting is off")
    check(mb_calls == [], f"and costs no lookup: {mb_calls}")

# the substitution set the pass walks: each entity on its own, whole-name first
queries = lp._alias_queries(JP_ARTIST, JP_TITLE, JP_ALBUM,
                            {"artist": [EN_ARTIST], "title": [EN_TITLE],
                             "album": [EN_ALBUM]})
check(queries and queries[0][:3] == (EN_ARTIST, EN_TITLE, JP_ALBUM),
      f"the whole name is localised first: {queries}")
check((EN_ARTIST, JP_TITLE, JP_ALBUM, "artist", EN_ARTIST) in queries,
      f"the artist alone may be substituted: {queries}")
check((JP_ARTIST, EN_TITLE, JP_ALBUM, "title", EN_TITLE) in queries,
      f"the title alone may be substituted: {queries}")
check((JP_ARTIST, JP_TITLE, EN_ALBUM, "album", EN_ALBUM) in queries,
      f"the album alone may be substituted: {queries}")
check(lp._alias_queries(JP_ARTIST, JP_TITLE, JP_ALBUM, {}) == (),
      "nothing to substitute is no second pass")
check(len(lp._alias_queries(JP_ARTIST, JP_TITLE, JP_ALBUM,
                            {"artist": [EN_ARTIST, "U", "X"],
                             "title": [EN_TITLE, "H", "Y"]})) <= 4,
      "the guess is capped")


# --------------------------------------------------------------------------- #
# 5. The manual search box (GET /api/lyrics/find) — same chain, no writes
# --------------------------------------------------------------------------- #
# The box carries no MusicBrainz ids, so its alias pass asks MusicBrainz by
# NAME, and only once the typed names have found nothing.
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server import api_lyrics  # noqa: E402

API = FastAPI()
API.include_router(api_lyrics.router)
CLIENT = TestClient(API)

mb_calls = []
EN_ANSWER = lp._hit(SYNCED_LRC, lp._lrc_to_plain(SYNCED_LRC),
                    EN_ARTIST, EN_TITLE, EN_ALBUM, None)
with Patch(api_lyrics, load_config=lambda: dict(CFG)), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub(
         {(JP_ARTIST, JP_TITLE): JP_SYNCED_HIT, (EN_ARTIST, EN_TITLE): EN_ANSWER},
         [], name="lrclib")}):
    r = CLIENT.get("/api/lyrics/find", params={"artist": JP_ARTIST,
                                               "title": JP_TITLE,
                                               "album": JP_ALBUM})
body = r.json()
check(r.status_code == 200 and body["found"] is True, body)
check(body["kind"] == "synced" and body["provider"] == "lrclib", body)
check("alias_pass" not in body, f"the typed names answered: {body}")
check(mb_calls == [], f"so MusicBrainz was never asked: {mb_calls}")

mb_calls = []
with Patch(api_lyrics, load_config=lambda: dict(CFG)), \
     Patch(si, mb_get_cached=fake_mb), \
     Patch(lp, _PROVIDERS={"lrclib": stub({(EN_ARTIST, EN_TITLE): EN_ANSWER},
                                          [], name="lrclib")}):
    r = CLIENT.get("/api/lyrics/find", params={"artist": JP_ARTIST,
                                               "title": JP_TITLE,
                                               "album": JP_ALBUM})
body = r.json()
check(body.get("found") is True and body.get("alias_pass"), body)
check(body["alias_pass"].get("entity") == "artist+title", body["alias_pass"])
check(mb_calls, "the second pass is what asked MusicBrainz")
check("wrote" not in body, "the box returns a HIT, never a write result")


# --------------------------------------------------------------------------- #
# 6. The ADVISORY route's alias fallback — original names first, always
# --------------------------------------------------------------------------- #
# `search_aliases` again (the same ordered names), turned into substitutions by
# the lyrics chain's own `_alias_queries`, and only the NAME-based sources
# re-asked: Apple's album route and song search (and the Discogs edition when a
# token is configured). The alias names are read only when the stored ones
# stated NOTHING.
ADV_TAGS = {"MUSICBRAINZ_TRACKID": "rec-1", "MUSICBRAINZ_ARTISTID": "art-1",
            "MUSICBRAINZ_RELEASEGROUPID": "rg-1"}
si._ADVISORY_CACHE.clear()
adv_calls = []


def fake_apple(artist, album, title="", disc=None, track=None, track_count=None,
               timeout=None, cfg=None):
    adv_calls.append(("apple-album", artist, album))
    return None


def fake_song(title, artist="", timeout=None):
    adv_calls.append(("itunes-song", artist, title))
    if (artist, title) == (EN_ARTIST, EN_TITLE):
        return (1, "itunes-song")
    return None


mb_calls = []
with Patch(si, _apple_album_advisory=fake_apple), \
     Patch(si, _itunes_song_advisory=fake_song), \
     Patch(si, mb_get_cached=fake_mb):
    route = si.resolve_advisory_route(title=JP_TITLE, artist=JP_ARTIST,
                                      album=JP_ALBUM, tags=ADV_TAGS,
                                      cfg={"locale": "en"})
check(route["value"] == 1,
      f"an advisory nobody stated under the stored names is found under the alias: {route}")
check(route["source"] == "itunes-song", route)
check(adv_calls[0][1:] == (JP_ARTIST, JP_ALBUM),
      f"the ORIGINAL names are asked first: {adv_calls}")
check(adv_calls[1][1:] == (JP_ARTIST, JP_TITLE),
      f"…and the original song search right after: {adv_calls}")
check((EN_ARTIST, EN_TITLE) in [c[1:] for c in adv_calls],
      f"the alias pair is asked only after they stated nothing: {adv_calls}")
check("artist/art-1" in mb_calls and "recording/rec-1" in mb_calls
      and "release-group/rg-1" in mb_calls,
      f"the aliases come from the ids the file carries: {mb_calls}")

# …and a source that DOES state something ends the question: not one alias name
# is asked, and MusicBrainz is never reached for them.
si._ADVISORY_CACHE.clear()
adv_calls, mb_calls = [], []


def fake_apple_hit(artist, album, title="", disc=None, track=None,
                   track_count=None, timeout=None, cfg=None):
    adv_calls.append(("apple-album", artist, album))
    return (1, "apple-album")


with Patch(si, _apple_album_advisory=fake_apple_hit), \
     Patch(si, _itunes_song_advisory=fake_song), \
     Patch(si, mb_get_cached=fake_mb):
    route = si.resolve_advisory_route(title=JP_TITLE, artist=JP_ARTIST,
                                      album=JP_ALBUM, tags=ADV_TAGS,
                                      cfg={"locale": "en"})
check(route["value"] == 1 and route["source"] == "apple-album", route)
check(mb_calls == [], f"a stated advisory costs no alias lookup: {mb_calls}")
check(not [c for c in adv_calls if c[1:] == (EN_ARTIST, EN_TITLE)],
      f"and no alias name is ever asked: {adv_calls}")

# A caller that gave no MusicBrainz ids gets no alias pass — and no request.
si._ADVISORY_CACHE.clear()
adv_calls, mb_calls = [], []
with Patch(si, _apple_album_advisory=fake_apple), \
     Patch(si, _itunes_song_advisory=fake_song), \
     Patch(si, mb_get_cached=fake_mb):
    route = si.resolve_advisory_route(title="Nobody", artist="Nobody At All",
                                      album="Nothing", cfg={})
check(route["value"] is None, route)
check(mb_calls == [], f"no ids is no MusicBrainz lookup at all: {mb_calls}")

# …and the IMPORT path really passes those ids, so a library-wide advisory run
# reaches the aliases too — the route is only half of the wiring
# (server/imports.fetch_advisories supplies the file's own ids).
from server import imports as _imports  # noqa: E402

si._ADVISORY_CACHE.clear()
adv_calls, mb_calls = [], []
adv_path = make_flac("advisory-alias.flac", dict(JP_TAGS))
with Patch(si, _apple_album_advisory=fake_apple), \
     Patch(si, _itunes_song_advisory=fake_song), \
     Patch(si, mb_get_cached=fake_mb):
    adv = _imports.fetch_advisories([adv_path], {"music_folder": TMP,
                                                 "advisory_fallback": "none"})
check(adv["values"].get(adv_path) == 1
      and adv["sources"].get(adv_path) == "itunes-song",
      f"an import's advisory step finds the track under its alias: {adv}")
check(adv_calls and adv_calls[0][1:] == (JP_ARTIST, JP_ALBUM),
      f"…the stored names first: {adv_calls}")
check((EN_ARTIST, EN_TITLE) in [c[1:] for c in adv_calls],
      f"…and the alias names through the ids the step passes: {adv_calls}")

# The recording id ALONE already reaches the TITLE's alias, with the artist
# alias absent: a Japanese-titled track Apple knows under its romanized title
# is rated instead of left unstated. (The artist id here is one MusicBrainz has
# no aliases for, so only the title substitution is available.)
si._ADVISORY_CACHE.clear()
adv_calls, mb_calls = [], []


def fake_song_title(title, artist="", timeout=None):
    adv_calls.append(("itunes-song", artist, title))
    return (1, "itunes-song") if title == EN_TITLE else None


title_path = make_flac("advisory-title-alias.flac",
                       dict(JP_TAGS, MUSICBRAINZ_ARTISTID="art-title-only"))
with Patch(si, _apple_album_advisory=fake_apple), \
     Patch(si, _itunes_song_advisory=fake_song_title), \
     Patch(si, mb_get_cached=fake_mb):
    adv = _imports.fetch_advisories([title_path], {"music_folder": TMP,
                                                   "advisory_fallback": "none"})
check(adv["values"].get(title_path) == 1,
      f"a title-only alias still rates the track: {adv}")
_song_calls = [c[1:] for c in adv_calls if c[0] == "itunes-song"]
check(_song_calls and _song_calls[0] == (JP_ARTIST, JP_TITLE),
      f"the stored title first: {_song_calls}")
check((JP_ARTIST, EN_TITLE) in _song_calls,
      f"then the alias title: {_song_calls}")


# --------------------------------------------------------------------------- #
# 7. The INSTRUMENTAL detector's alias fallback (the same rule, same helpers)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


si._ADVISORY_CACHE.clear()
inst_calls = []


def fake_lrclib(endpoint, params, timeout=15, retries=3):
    inst_calls.append((endpoint, dict(params)))
    if (params.get("artist_name") == EN_ARTIST
            and params.get("track_name") == EN_TITLE):
        return _Resp({"instrumental": True, "trackName": EN_TITLE,
                      "duration": 1})
    return _Resp({"message": "not found"}, 404)


inst_path = make_flac("instr-alias-hit.flac", dict(JP_TAGS))
mb_calls = []
with Patch(si, mb_get_cached=fake_mb), Patch(si, _lrclib_get=fake_lrclib):
    got = inst.detect_instrumental([inst_path], CFG)[inst_path]
check(got["value"] == 1 and got["answers"] == {"lrclib": 1},
      f"LRCLIB's instrumental flag is found under the alias name: {got}")
check(inst_calls[0][1].get("track_name") == JP_TITLE,
      f"the original name is tried first: {inst_calls}")
check(any(c[1].get("track_name") == EN_TITLE for c in inst_calls),
      f"the alias name is what answered: {inst_calls}")
check(mb_calls, f"the alias names came from the file's MusicBrainz ids: {mb_calls}")

# …and without ids the lookup is exactly the one original pass, no MB request.
si._ADVISORY_CACHE.clear()
inst_calls, mb_calls = [], []
plain_path = make_flac("instr-no-ids.flac", {"ARTIST": "Rush", "TITLE": "YYZ"})
with Patch(si, mb_get_cached=fake_mb), Patch(si, _lrclib_get=fake_lrclib):
    got = inst.detect_instrumental([plain_path], CFG)[plain_path]
check(got["value"] is None, got)
check(mb_calls == [], f"no MusicBrainz ids costs no MB lookup: {mb_calls}")
check({c[1].get("artist_name") for c in inst_calls} == {"Rush"},
      f"and only the stored name is asked (get then search): {inst_calls}")


# --------------------------------------------------------------------------- #
# 8. Publishing under every alias name pair (script 18 / the batch route)
# --------------------------------------------------------------------------- #
from mlo import lyrics_publish as pub  # noqa: E402

si._ADVISORY_CACHE.clear()
pub_path = make_flac("publish-alias.flac",
                     dict(JP_TAGS, LYRICS="[00:01.00]Hello there"))
submitted = []


def fake_pub(artist, track, album, duration, plain=None, synced=None, solver=None):
    submitted.append((artist, track, album))
    return True, "published to LRCLIB — thank you for contributing!"


mb_calls = []
_real_fetch, _real_publish = pub.lrclib_fetch, pub.lrclib_publish
pub.lrclib_fetch = lambda *a, **k: None
pub.lrclib_publish = fake_pub
try:
    with Patch(si, mb_get_cached=fake_mb):
        got = pub.publish_one(pub_path, dict(CFG, lyrics_search_aliases=True))
finally:
    pub.lrclib_fetch, pub.lrclib_publish = _real_fetch, _real_publish
check(len(submitted) >= 2,
      f"a track with aliases is published under more than one name pair: {submitted}")
check(submitted[0] == (JP_ARTIST, JP_TITLE, JP_ALBUM),
      f"the original pair goes first: {submitted}")
check((EN_ARTIST, EN_TITLE, JP_ALBUM) in submitted,
      f"and the localized alias pair follows: {submitted}")
check(got["status"] == "ok", got)
check(len(got.get("names") or []) == len(submitted),
      f"every pair is reported with its own outcome: {got.get('names')}")
check(all(n["status"] == "ok" for n in got["names"]), got["names"])

# INDEPENDENT outcomes: a duplicate under the alias name is that pair's skip,
# never a failure of the original (and never a reason to stop).
si._ADVISORY_CACHE.clear()
pub_path2 = make_flac("publish-alias-dup.flac",
                      dict(JP_TAGS, LYRICS="[00:01.00]Hello there"))
submitted = []


def fake_pub_dup(artist, track, album, duration, plain=None, synced=None, solver=None):
    submitted.append((artist, track, album))
    if (artist, track) == (EN_ARTIST, EN_TITLE):
        return False, "LRCLIB already has this track"
    return True, "published to LRCLIB — thank you for contributing!"


pub.lrclib_fetch = lambda *a, **k: None
pub.lrclib_publish = fake_pub_dup
try:
    with Patch(si, mb_get_cached=fake_mb):
        got = pub.publish_one(pub_path2, dict(CFG))
finally:
    pub.lrclib_fetch, pub.lrclib_publish = _real_fetch, _real_publish
_pairs = {f"{n['artist']}·{n['title']}": n for n in got.get("names") or []}
check(got["status"] == "ok", f"one duplicate does not stop the others: {got}")
check(_pairs.get(f"{EN_ARTIST}·{EN_TITLE}", {}).get("status") == "skipped",
      f"the duplicate name is a skip for itself: {_pairs.get(f'{EN_ARTIST}·{EN_TITLE}')}")
check(_pairs.get(f"{JP_ARTIST}·{JP_TITLE}", {}).get("status") == "ok",
      f"…while the original still publishes: {_pairs.get(f'{JP_ARTIST}·{JP_TITLE}')}")

# The switch is the same one the lyrics chain honours: off, one pair only.
si._ADVISORY_CACHE.clear()
pub_path3 = make_flac("publish-alias-off.flac",
                      dict(JP_TAGS, LYRICS="[00:01.00]Hello there"))
submitted = []
pub.lrclib_fetch = lambda *a, **k: None
pub.lrclib_publish = fake_pub
try:
    with Patch(si, mb_get_cached=fake_mb):
        got = pub.publish_one(pub_path3, dict(CFG, lyrics_search_aliases=False))
finally:
    pub.lrclib_fetch, pub.lrclib_publish = _real_fetch, _real_publish
check(submitted == [(JP_ARTIST, JP_TITLE, JP_ALBUM)] and len(mb_calls) >= 0,
      f"lyrics_search_aliases off publishes the original pair alone: {submitted}")

# …and the BATCH ROUTE reports every name's own outcome: `publish_one`'s
# `names` rides through `server/api_lyrics.lyrics_publish_batch` (the result
# dict it spreads), so the editor's batch panel can say which names landed.
si._ADVISORY_CACHE.clear()
submitted = []
pub.lrclib_fetch = lambda *a, **k: None
pub.lrclib_publish = fake_pub
try:
    with Patch(api_lyrics, load_config=lambda: dict(CFG)), \
         Patch(si, mb_get_cached=fake_mb):
        _r = CLIENT.post("/api/lyrics/publish-batch",
                         json={"paths": [pub_path]})
finally:
    pub.lrclib_fetch, pub.lrclib_publish = _real_fetch, _real_publish
_body = _r.json()
_names = ((_body.get("results") or [{}])[0] or {}).get("names") or []
check(_r.status_code == 200 and _body.get("ok") == 1,
      f"the batch route publishes and counts the track: {_body}")
check(any(n.get("artist") == EN_ARTIST and n.get("title") == EN_TITLE
          for n in _names) and any(n.get("artist") == JP_ARTIST for n in _names),
      f"…and reports every name pair with its own outcome: {_names}")


if failures:
    for f in failures:
        print("FAIL:", f)
    raise SystemExit(1)
print(f"ok — {checks} checks: plain fallback + alias pass in the lyrics chain, "
      f"the advisory route, the instrumental detector and publishing")