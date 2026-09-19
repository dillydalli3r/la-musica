#!/usr/bin/env python3
"""Genre formatting and AI ranking — offline contract, no network at all.

What this pins:

  * `mlo.genres.normalize_genres` is THE list policy: every name resolved to
    MusicBrainz's own spelling when MusicBrainz publishes it (and kept verbatim
    when it does not — dropping what a source said would be worse), duplicates
    collapsed on the casefolded name, the FAMILY derived from the first
    specific genre and moved to the LAST slot, the result capped at the
    configured count, and a name that IS a family used as the family slot
    rather than repeated as a specific genre;
  * `format_genres` / `split_stored` round-trip: the rendered
    "shoegaze / rock" string reads back as exactly the names inside it, which
    is what lets the display spelling (and the one another tagger stores) be
    read back;
  * `issues` reports the structural problems — too many, a duplicate, a family
    that is not last, spacing, a name MusicBrainz does not publish — and
    nothing else;
  * `server.genre_ai.infer_genres` asks for at most `count - 1` SPECIFIC
    genres (the family is the app's to append), names the release and the
    fetched candidates in its prompt, states the family rule, honours
    `ai_genre_research` (own knowledge vs re-rank only) and `ai_genre_effort`
    (passed to the shared client as `ai_effort`), parses a JSON object, a
    fenced block, a bare array and a plain newline list, and returns None —
    never raising — for an unconfigured client, a failing call, a refusal, a
    single-family answer, a name it invented, and `count == 1` (no specific
    slot exists then);
  * the same question is answered from the disk cache on the second call, so
    an import that re-runs costs no second request;
  * `mlo.autotag.trim_genres` routes through `normalize_genres`: a
    case-insensitive duplicate and a non-canonical spelling are fixed as well
    as the overflow, the family lands last, its return value is still "how
    many values were removed", and a list that is already canonical is never
    rewritten;
  * `server.integrations.genre_chain` lets the AI answer REPLACE the merged
    source list when `ai_genre_inference` is on — canonicalized into the full
    specific/family list — names "ai" among the path's contributing sources,
    and leaves every field alone when it is off or the ranker raises.

Run:  python tools/test_genre_format.py   (exit 0 = pass, 1 = failure)
"""
import os
import sys
import tempfile
from itertools import count as _count

_SEQ = _count(1)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import autotag
from mlo import genres as G
from server import ai as ai_mod
from server import genre_ai
from server import integrations as intg

# --------------------------------------------------------------------------- #
# 1) The list policy: canonical names, case-insensitive duplicates, cap
# --------------------------------------------------------------------------- #
# Specific genres first, their family LAST — and every name in MusicBrainz's
# own spelling (lowercase: that is how MusicBrainz publishes them).
assert G.normalize_genres(["shoegaze", "rock"]) == ["shoegaze", "rock"]
# A family in the wrong slot is moved, never dropped, and a family elsewhere in
# the list is not repeated as a specific genre.
assert G.normalize_genres(["rock", "shoegaze"]) == ["shoegaze", "rock"]
assert G.normalize_genres(["shoegaze", "dream pop", "rock"], 3) == [
    "shoegaze", "dream pop", "rock"]
# Spelling is resolved to the vocabulary's own: "Shoegaze" -> "shoegaze", and
# the alias table runs too.
assert G.normalize_genres(["Shoegaze", "Rock"]) == ["shoegaze", "rock"]
# Duplicates collapse on the casefolded name, the FIRST spelling surviving.
assert G.normalize_genres(["Shoegaze", "shoegaze", "SHOEGAZE", "IDM"], 3) == [
    "shoegaze", "idm", "rock"]
assert G.normalize_genres(["shoegaze", "  ", "idm"], 3) == [
    "shoegaze", "idm", "rock"]
# The family is DERIVED and takes the last slot; the cap decides how many
# specific genres sit in front of it.
assert G.normalize_genres(["shoegaze", "dream pop"], 3) == [
    "shoegaze", "dream pop", "rock"]
assert G.normalize_genres(["shoegaze", "dream pop"], 2) == ["shoegaze", "rock"]
# One slot has no room for both, and the specific genre is the informative one.
assert G.normalize_genres(["shoegaze", "dream pop"], 1) == ["shoegaze"]
# A genre with no known family simply has no family slot…
assert G.normalize_genres(["Nonsense"]) == ["Nonsense"]
# …and an unrecognised name is kept VERBATIM rather than dropped: the grade
# check flags it (mlo.grader.grade_check_genre_vocab), which is honest.
assert G.normalize_genres(["shoegaze", "Nonsense"], 3) == [
    "shoegaze", "Nonsense", "rock"]
# A single stored value another tagger joined is read as the names inside it.
assert G.split_stored("Rock; Shoegaze") == ["Rock", "Shoegaze"]
assert G.split_stored("shoegaze / rock") == ["shoegaze", "rock"]

# --------------------------------------------------------------------------- #
# 2) format_genres / split_stored round trip
# --------------------------------------------------------------------------- #
text = G.format_genres(["shoegaze"])
assert text == "shoegaze / rock", text
assert G.split_stored(text) == ["shoegaze", "rock"], G.split_stored(text)
assert G.split_stored("shoegaze") == ["shoegaze"]
assert G.split_stored("") == []
assert G.format_genres([]) == ""
# Reading our own render back through the normalizer is lossless — the
# property that lets the " / " spelling be stored by another tagger.
assert G.normalize_genres([text]) == ["shoegaze", "rock"]

# --------------------------------------------------------------------------- #
# 3) issues(): the structural problems, and nothing else
# --------------------------------------------------------------------------- #
assert G.issues(["shoegaze", "rock"]) == []
assert G.issues(["shoegaze", "dream pop", "rock"], 3) == []
assert G.issues([]) == ["missing"]
assert G.issues(["shoegaze", "dream pop", "rock"], 2) == ["too many (3 > 2)"]
assert G.issues(["shoegaze", "shoegaze"]) == ["duplicate"]
# The family is the LAST slot; anywhere else is a hierarchy problem.
assert G.issues(["rock", "shoegaze"]) == ["family genre must be the last one"]
# A name the vocabulary does not publish is its own line (the writer kept it,
# the grader reports it). Surrounding and internal whitespace is not a problem
# this reports: the names arrive already cleaned by split_stored/_clean.
assert G.issues(["Nonsense"]) == ["not a known genre: Nonsense"]
assert G.issues(["  shoegaze  ", "rock"]) == []
assert G.issues("  shoegaze  ") == []

# --------------------------------------------------------------------------- #
# 4) infer_genres: prompt, parsing, gating — with the chat client stubbed
# --------------------------------------------------------------------------- #
CFG = {"ai_base_url": "http://127.0.0.1:9/v1", "ai_model": "test-model",
       "ai_genre_effort": "low", "ai_genre_research": True}
TMP = tempfile.mkdtemp(prefix="mlo-genre-ai-")
_real_chat, _real_config = ai_mod.ai_chat, genre_ai._config
_real_cache_dir = ai_mod._cache_dir


class FakeChat:
    """`server.ai.ai_chat`, recording what the genre ranker asked for."""

    def __init__(self, answer="", boom=False):
        self.answer, self.boom, self.calls = answer, boom, []

    def __call__(self, config, system, user, timeout=90.0):
        self.calls.append((dict(config), system, user))
        if self.boom:
            raise RuntimeError("endpoint down")
        return self.answer


def ask(chat, candidates=None, **cfg):
    """One infer_genres call against the stub, returning (names, prompt).

    The album gets a fresh number per call: the disk cache is keyed by the
    prompt, and these cases each need their OWN request rather than the
    previous answer served again. *candidates* defaults to the two fetched
    genres the other cases use.
    """
    n = next(_SEQ)
    ai_mod.ai_chat = chat
    genre_ai._config = lambda: dict(CFG, **cfg)
    got = genre_ai.infer_genres(
        artist="Radiohead", album=f"OK Computer #{n}", title="Paranoid Android",
        candidates=list(candidates or ["shoegaze", "dream pop"]), count=3)
    prompt = chat.calls[0][2] if chat.calls else ""
    return got, prompt


try:
    # The shared cache root is the REAL app-data folder; keep this run's
    # answers out of it.
    ai_mod._cache_dir = lambda: TMP

    chat = FakeChat('{"genres": ["Post-Britpop", "Shoegaze"]}')
    ai_mod.ai_chat = chat
    FIXED = dict(artist="Radiohead", album="OK Computer",
                 title="Paranoid Android",
                 candidates=["shoegaze", "dream pop"], count=3)
    genre_ai._config = lambda: dict(CFG)
    got = genre_ai.infer_genres(**FIXED)
    prompt = chat.calls[0][2]
    # count=3 means two SPECIFIC slots: the model's own order is the ranking,
    # each name is resolved to MusicBrainz's spelling, and no family comes back.
    assert got == ["post-britpop", "shoegaze"], got
    # The prompt names the release, the candidates and the shape asked for.
    assert "Radiohead" in prompt and "OK Computer" in prompt, prompt
    assert "Paranoid Android" in prompt, prompt
    assert "shoegaze" in prompt and "dream pop" in prompt, prompt
    assert "AT MOST 2 SPECIFIC" in prompt, prompt
    assert "Do NOT name a broad family" in prompt, prompt
    assert "JSON" in prompt, prompt
    # research on: the model is told it may go past the fetched list.
    assert "your own knowledge" in prompt, prompt
    # The genre thinking budget is what the shared client is handed.
    assert chat.calls[0][0]["ai_effort"] == "low", chat.calls[0][0]
    assert chat.calls[0][0]["ai_model"] == "test-model", chat.calls[0][0]

    # Same question again: answered from the cache, no second request — even
    # with a client that would answer something else.
    ai_mod.ai_chat = FakeChat('{"genres": ["trip hop"]}')
    assert genre_ai.infer_genres(**FIXED) == got
    assert ai_mod.ai_chat.calls == [], ai_mod.ai_chat.calls
    # A different question (new candidates) is a different cache key, and the
    # ask grows with the count: three slots allow two specifics.
    ai_mod.ai_chat = FakeChat('{"genres": ["Dream Pop"]}')
    other = genre_ai.infer_genres(
        artist="Slowdive", album="Souvlaki",
        candidates=["shoegaze", "dream pop"], count=3)
    assert other == ["dream pop"], other
    assert len(ai_mod.ai_chat.calls) == 1, ai_mod.ai_chat.calls

    # research off: re-rank the fetched list only.
    _, prompt = ask(FakeChat('{"genres": ["Shoegaze"]}'),
                    ai_genre_research=False)
    assert "Re-rank ONLY the fetched genres" in prompt, prompt
    assert "your own knowledge" not in prompt, prompt

    # The answer shapes a model actually returns — all accepted.
    for answer in ('```json\n{"genres": ["Shoegaze", "Dream Pop"]}\n```',
                   '["Shoegaze", "Dream Pop"]',
                   'Shoegaze\nDream Pop',
                   '1. Shoegaze\n2. Dream Pop',
                   '```\n{"genres": ["Shoegaze", "Dream Pop"]}\n```'):
        got, _ = ask(FakeChat(answer))
        assert got == ["shoegaze", "dream pop"], (answer, got)
    # …and the answer is canonicalized: a repeat and a blank are dropped.
    got, _ = ask(FakeChat('{"genres": ["Shoegaze", "shoegaze", "", "IDM"]}'))
    assert got == ["shoegaze", "idm"], got

    # A FAMILY is not a specific genre, so a family answer is dropped — the app
    # appends the family itself. With count=2 there is one specific slot.
    got, _ = ask(FakeChat('{"genres": ["Rock", "Shoegaze"]}'), count=2)
    assert got == ["shoegaze"], got
    got, _ = ask(FakeChat('{"genres": ["Rock"]}'), count=2)
    assert got is None, got

    # A name that is neither a MusicBrainz genre nor a fetched candidate is an
    # invention, not an answer…
    got, _ = ask(FakeChat('{"genres": ["Vyrzukhisuc-Art"]}'))
    assert got is None, got
    # …while a fetched candidate is kept as the source spelled it: a source's
    # own name is evidence (the writers canonicalize, the grade flags what
    # MusicBrainz does not publish), so the model may repeat it.
    got, _ = ask(FakeChat('{"genres": ["Nonsense"]}'),
                 candidates=["Nonsense", "shoegaze"])
    assert got == ["Nonsense"], got

    # A refusal / prose answer has no usable names: None, never a stored junk
    # genre. Same for an empty object and an answer with no specific genre.
    for answer in ("I can't help with that.", "", "```json\n{}\n```"):
        got, _ = ask(FakeChat(answer))
        assert got is None, (answer, got)

    # count=1 is the family-only slot: there is no specific genre to ask for, so
    # the model is never called at all.
    ai_mod.ai_chat = FakeChat('{"genres": ["Shoegaze"]}')
    genre_ai._config = lambda: dict(CFG)
    assert genre_ai.infer_genres(artist="A", album="B", count=1) is None
    assert ai_mod.ai_chat.calls == [], ai_mod.ai_chat.calls

    # A failing endpoint and an unconfigured client are both "no opinion".
    got, _ = ask(FakeChat(boom=True))
    assert got is None, got
    ai_mod.ai_chat = FakeChat('{"genres": ["Shoegaze"]}')
    genre_ai._config = lambda: {"ai_genre_research": True}
    assert genre_ai.infer_genres(artist="A", album="B") is None
    assert ai_mod.ai_chat.calls == [], ai_mod.ai_chat.calls
finally:
    ai_mod.ai_chat, genre_ai._config = _real_chat, _real_config
    ai_mod._cache_dir = _real_cache_dir
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

# --------------------------------------------------------------------------- #
# 5) trim_genres: the duplicate and the spelling go, the family lands last
# --------------------------------------------------------------------------- #
_MISSING = object()


class FakeAudioFile:
    """The three methods `trim_genres` uses, and nothing else — a real audio
    container is not needed to test a list policy."""

    def __init__(self, values):
        self.values = None if values is None else list(values)
        self.written = _MISSING
        self.deleted = False

    def tag_values(self, key):
        return [] if self.values is None else list(self.values)

    def get_tag(self, key):
        return None if self.values is None else self.values[0]

    def set_tag(self, key, value):
        self.values = list(value) if isinstance(value, list) else [value]
        self.written = value
        return True

    def delete_tag(self, key):
        self.deleted, self.values = True, None
        return True


# A case-insensitive duplicate goes as well as the overflow, and the derived
# family takes the slot in front of it.
af = FakeAudioFile(["shoegaze", "shoegaze", "dream pop"])
removed = autotag.trim_genres(af, 2)
assert removed == 1, removed
assert af.values == ["shoegaze", "rock"], af.values
assert af.written == ["shoegaze", "rock"], af.written

# Already canonical (family last, MusicBrainz's own spelling): nothing is
# removed and the container is never rewritten.
af = FakeAudioFile(["shoegaze", "rock"])
assert autotag.trim_genres(af, 2) == 0
assert af.written is _MISSING, af.written

# Over the cap with no duplicates: the overflow goes, and the family takes the
# last slot (three names in, two stored, one value removed).
af = FakeAudioFile(["shoegaze", "dream pop", "post-britpop"])
assert autotag.trim_genres(af, 2) == 1
assert af.values == ["shoegaze", "rock"], af.values

# One stored value holding a joined list is split and canonicalized — the file
# another tagger wrote comes out as the names it holds, one field each.
af = FakeAudioFile(["Rock; Shoegaze"])
assert autotag.trim_genres(af, 2) == 0     # two names in, two names out
assert af.values == ["shoegaze", "rock"], af.values
# Under the cap that already holds the canonical spelling there is nothing to
# fix at all (the split is this helper's reading, not a rewrite it owes).
af = FakeAudioFile(["shoegaze; rock"])
assert autotag.trim_genres(af, 5) == 0
assert af.written is _MISSING, af.written

# count=0 still means "keep none" (an empty GENRE is worse than no GENRE).
af = FakeAudioFile(["shoegaze", "rock"])
assert autotag.trim_genres(af, 0) == 2
assert af.deleted and af.values is None, af.values

# No GENRE tag at all: nothing to do.
af = FakeAudioFile(None)
assert autotag.trim_genres(af, 3) == 0
assert not af.deleted, af.deleted

# --------------------------------------------------------------------------- #
# 6) the one cap: cfg -> genre_count(), the wizard's limit may only lower it
# --------------------------------------------------------------------------- #
assert autotag.genre_count({}) == 2                       # shipped default
assert autotag.genre_count({"mb_genre_count": 3}) == 3
assert autotag.genre_count({"mb_genre_count": 9}) == 3    # the ceiling
assert autotag.genre_count({"mb_genre_count": 0}) == 2    # 0/empty = unset
assert autotag.genre_count({"mb_genre_count": "x"}) == 2  # unparsable -> default
assert autotag.genre_count({"mb_genre_count": 3}, 1) == 1  # a request lowers
assert autotag.genre_count({"mb_genre_count": 2}, 5) == 2  # never raises

# --------------------------------------------------------------------------- #
# 7) genre_chain hands the merged list to the AI and replaces it on an answer
# --------------------------------------------------------------------------- #
RELEASE = {"id": "rel-1", "title": "Test Album", "date": "1997-05-21",
           "country": "GB",
           "artists": [{"name": "Test Artist", "mbid": "art-1"}],
           "media": [{"disc": 1, "position": 1, "title": "Track One",
                      "genres": []}]}
_real_sources = intg._genre_source_answers
_real_infer = genre_ai.infer_genres
# An endpoint is configured, so the ranker is actually reached (the gate needs
# both the setting and a client).
ON = {"mb_genre_count": 3, "genre_sources": ["musicbrainz"],
      "ai_genre_inference": True, "ai_base_url": "http://127.0.0.1:9/v1",
      "ai_model": "test-model"}
try:
    # One source, album-wide, so the merged candidates are known exactly.
    intg._genre_source_answers = lambda *a, **k: {
        intg._ALL_TRACKS: {"level": "album", "genres": ["shoegaze", "dream pop"]}}
    seen = []

    def fake_infer(*, artist, album, title="", track_path="", candidates=None,
                   count=3, extra=None):
        seen.append((artist, album, title, list(candidates or []), count, extra))
        # Specifics only, in the model's own casing and order (and with a blank
        # and a repeat on purpose): the chain canonicalizes them into the full
        # list — the family is the app's to append.
        return ["Post-Britpop", "shoegaze", "", "shoegaze"]

    genre_ai.infer_genres = fake_infer
    on = intg.genre_chain(artist="Test Artist", album="Test Album",
                          release=RELEASE, files=[], cfg=dict(ON))
    assert on["per_track"][(1, 1)] == ["post-britpop", "shoegaze", "rock"], on["per_track"]
    assert on["genres"] == ["post-britpop", "shoegaze", "rock"], on["genres"]
    assert on["per_track_sources"][(1, 1)] == ["musicbrainz", "ai"], on["per_track_sources"]
    # The ranker was asked with the release identity, the sources' own
    # candidates, the cap and the release context.
    assert seen and seen[0][:2] == ("Test Artist", "Test Album"), seen
    assert seen[0][2] == "Track One", seen
    assert seen[0][3] == ["shoegaze", "dream pop"], seen
    assert seen[0][4] == 3, seen
    assert seen[0][5].get("year") == "1997", seen
    assert seen[0][5].get("country") == "GB", seen

    # The setting off: the ranker is not reached at all, and every field is
    # what the sources alone produced (the merge keeps their spelling — the
    # WRITERS canonicalize).
    seen.clear()
    quiet = intg.genre_chain(artist="Test Artist", album="Test Album",
                             release=RELEASE, files=[],
                             cfg={"mb_genre_count": 3,
                                  "genre_sources": ["musicbrainz"]})
    assert seen == [], seen
    assert quiet["per_track"][(1, 1)] == ["shoegaze", "dream pop"], quiet["per_track"]
    assert quiet["per_track_sources"][(1, 1)] == ["musicbrainz"], quiet["per_track_sources"]

    # A ranker that raises is swallowed whole: a broken endpoint must never
    # fail the import it feeds.
    def dead_infer(**kwargs):
        raise RuntimeError("no endpoint")

    genre_ai.infer_genres = dead_infer
    off = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=RELEASE, files=[], cfg=dict(ON))
    assert off["per_track"][(1, 1)] == ["shoegaze", "dream pop"], off["per_track"]
    assert off["per_track_sources"][(1, 1)] == ["musicbrainz"], off["per_track_sources"]
    assert off["genres"] == ["shoegaze", "dream pop"], off["genres"]
finally:
    intg._genre_source_answers = _real_sources
    genre_ai.infer_genres = _real_infer

print("genre format: all assertions passed")
sys.exit(0)
