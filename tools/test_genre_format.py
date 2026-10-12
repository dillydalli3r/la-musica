#!/usr/bin/env python3
"""Genre formatting and AI ranking — offline contract, no network at all.

What this pins:

  * `mlo.genres.normalize_genres` is THE list policy: every name resolved to
    MusicBrainz's own spelling when MusicBrainz publishes it (and kept verbatim
    when it does not — dropping what a source said would be worse), duplicates
    collapsed on the casefolded name, the result capped at the configured count
    (the FIRST N names, the source's own rank) and kept in the source's own
    order — nothing is ever added and nothing is dropped for being a parent;
  * `format_genres` / `split_stored` round-trip: the rendered
    "rock / shoegaze" string reads back as exactly the names inside it, which
    is what lets the display spelling (and the one another tagger stores) be
    read back;
  * `issues` reports the structural problems — too many, a duplicate, spacing
    and a name MusicBrainz does not publish — and nothing else; neither the
    order nor a parent stored beside its own child is a defect any more;
  * `server.genre_ai.infer_genres` asks for at most `count` genres (every slot
    is a genre now), names the release and the fetched candidates in its
    prompt, honours `ai_genre_research` (own knowledge vs re-rank only) and
    `ai_genre_effort` (passed to the shared client as `ai_effort`), parses a
    JSON object, a fenced block, a bare array and a plain newline list, and
    returns None — never raising — for an unconfigured client, a failing call,
    a refusal, an answer with no usable name, and a name it invented;
  * the same question is answered from the disk cache on the second call, so
    an import that re-runs costs no second request;
  * `mlo.autotag.trim_genres` routes through `normalize_genres`: a
    case-insensitive duplicate and a non-display spelling are fixed as well as
    the overflow, the surviving names KEEP the file's own order (the source's
    rank), its return value is still "how many values were removed", and a list
    that is already in shape is never rewritten;
  * `server.integrations.genre_chain` asks the AI only as a LAST RESORT: when
    the sources already fill every slot the writer would write, no model call
    is spent (a mere disagreement between them is not a reason to call it);
    only when they cannot fill the slots does the call happen, and a usable
    answer REPLACES the merged source list — canonicalized but in the model's
    own order — names "ai" among the path's contributing sources,
    and leaves every field alone when the setting is off or the ranker raises.

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
# The names a source STATED, resolved to MusicBrainz's own genre names and
# CAPITALIZED for display (mlo.genres.display_name): the vocabulary keeps them
# lowercase, a tag is read by a person. Nothing is ever ADDED and nothing is
# dropped for being a parent (the owner's call, 2026-10-12) — the ORDER is the
# source's own (MusicBrainz publishes an entity's genres by vote count,
# most-voted first), and the cap keeps the FIRST N names (count defaults to 2:
# the top two genres the track's own entity lists).
assert G.normalize_genres(["shoegaze", "rock"]) == ["Shoegaze", "Rock"]
assert G.normalize_genres(["idm", "shoegaze"]) == ["IDM", "Shoegaze"]
assert G.normalize_genres(["shoegaze", "dream pop", "idm"], 3) == [
    "Shoegaze", "Dream Pop", "IDM"]
# Spelling is resolved to the vocabulary's own first: any casing of "Shoegaze"
# resolves (the vocabulary lookup folds case) and the alias table runs too.
assert G.normalize_genres(["Shoegaze", "Rock"]) == ["Shoegaze", "Rock"]
# A parent name the source stated is kept exactly where the source put it: the
# rule about parents is GONE and no name is derived above another.
assert G.normalize_genres(["Rock", "Shoegaze"]) == ["Rock", "Shoegaze"]
# Duplicates collapse on the casefolded name, the FIRST spelling surviving.
assert G.normalize_genres(["Shoegaze", "shoegaze", "SHOEGAZE", "IDM"], 3) == [
    "Shoegaze", "IDM"]
assert G.normalize_genres(["shoegaze", "  ", "idm"], 3) == ["Shoegaze", "IDM"]
# The cap keeps the FIRST N names and only reorders nothing: they ARE the
# source's rank, so the tail goes and the head stays in place.
assert G.normalize_genres(["shoegaze", "dream pop"], 3) == [
    "Shoegaze", "Dream Pop"]
assert G.normalize_genres(["shoegaze", "dream pop"], 2) == ["Shoegaze", "Dream Pop"]
assert G.normalize_genres(["shoegaze", "dream pop"], 1) == ["Shoegaze"]
# A 3-name list with count=2 keeps the first two, in the source's order.
assert G.normalize_genres(["shoegaze", "dream pop", "idm"], 2) == [
    "Shoegaze", "Dream Pop"]
# A genre with no known parent sits at the top of its own chain…
assert G.normalize_genres(["Nonsense"]) == ["Nonsense"]
# …and an unrecognised name is kept VERBATIM rather than dropped: the grade
# check flags it (mlo.grader.grade_check_genre_vocab), which is honest.
assert G.normalize_genres(["shoegaze", "Nonsense"], 3) == [
    "Shoegaze", "Nonsense"]
# A parent+child pair a source really stated is kept VERBATIM, in the source's
# own order — the old app-written family slot is no longer a thing, and neither
# name is invented or removed.
assert G.normalize_genres(["rock", "shoegaze"]) == ["Rock", "Shoegaze"]
assert G.normalize_genres(["shoegaze", "rock"]) == ["Shoegaze", "Rock"]
assert G.normalize_genres(["pop", "art pop"]) == ["Pop", "Art Pop"]
assert G.normalize_genres(["art pop", "pop"]) == ["Art Pop", "Pop"]
assert G.normalize_genres(["pop", "art pop", "synth-pop"], 3) == [
    "Pop", "Art Pop", "Synth-Pop"]
assert G.normalize_genres(["pop", "art pop", "house"], 3) == [
    "Pop", "Art Pop", "House"]                                      # three names
# Two orders of the same pair are two lists now: there is no canonical order,
# so the source's own survives whichever way it arrived.
assert G.normalize_genres(["trip hop", "alternative rock"], 3) == [
    "Trip Hop", "Alternative Rock"]
assert G.normalize_genres(["alternative rock", "trip hop"], 3) == [
    "Alternative Rock", "Trip Hop"]                           # either arrival order
assert G.normalize_genres(["rock", "jazz"]) == ["Rock", "Jazz"]
assert G.normalize_genres(["rock"]) == ["Rock"]
# An unrecognised name is neither resolved nor dropped: it is kept verbatim and
# never treated as an ancestor of anything (it lands after the known names).
assert G.normalize_genres(["Nonsense", "Pop", "Electropop"], 3) == [
    "Pop", "Electropop", "Nonsense"]
# A single stored value another tagger joined is read as the names inside it.
assert G.split_stored("Rock; Shoegaze") == ["Rock", "Shoegaze"]
assert G.split_stored("shoegaze / rock") == ["shoegaze", "rock"]

# --------------------------------------------------------------------------- #
# 2) format_genres / split_stored round trip
# --------------------------------------------------------------------------- #
text = G.format_genres(["shoegaze"])
assert text == "Shoegaze", text
assert G.split_stored(text) == ["Shoegaze"], G.split_stored(text)
assert G.format_genres(["shoegaze", "dream pop"]) == "Shoegaze / Dream Pop"
assert G.split_stored("shoegaze") == ["shoegaze"]
assert G.split_stored("") == []
assert G.format_genres([]) == ""
# Reading our own render back through the normalizer is lossless — the
# property that lets the " / " spelling be stored by another tagger.
assert G.normalize_genres([text]) == ["Shoegaze"]
assert G.normalize_genres([G.format_genres(["shoegaze", "dream pop"])]) == [
    "Shoegaze", "Dream Pop"]

# --------------------------------------------------------------------------- #
# 3) issues(): the structural problems, and nothing else
# --------------------------------------------------------------------------- #
assert G.issues(["idm", "shoegaze"]) == []
assert G.issues(["dream pop", "idm", "shoegaze"], 3) == []
assert G.issues([]) == ["missing"]
assert G.issues(["dream pop", "idm", "shoegaze"], 2) == ["too many (3 > 2)"]
assert G.issues(["shoegaze", "shoegaze"]) == ["duplicate"]
# ORDER carries no defect any more: the list IS the source's rank, so no grader
# can say what another order should have been; a PARENT sitting beside its own
# child is just another name too (the owner's call, 2026-10-12). What is left
# is a repeat, an unknown spelling, spacing and too many names.
assert G.issues(["Alternative Rock", "Trip Hop"]) == []
assert G.issues(["Trip Hop", "Alternative Rock"]) == []          # either order
assert G.issues(["rock", "shoegaze"]) == []
assert G.issues(["shoegaze", "rock"]) == []                      # …and reversed
assert G.issues(["Pop", "Art Pop"]) == []
assert G.issues(["art pop", "pop"]) == []
assert G.issues(["pop", "art pop", "house"], 3) == []
assert G.issues(["jazz", "rock"]) == []
assert G.issues(["alternative rock", "trip hop"]) == []
assert G.issues(["rock"]) == []
# …and only a REPEAT fails, wherever it sits and whatever its casing.
assert G.issues(["rock", "rock"]) == ["duplicate"]
assert G.issues(["Shoegaze", "shoegaze"]) == ["duplicate"]
# A name the vocabulary does not publish is its own line (the writer kept it,
# the grader reports it). Surrounding and internal whitespace is not a problem
# this reports: the names arrive already cleaned by split_stored/_clean.
assert G.issues(["Nonsense"]) == ["not a known genre: Nonsense"]
assert G.issues(["idm", "  shoegaze  "]) == []
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
    # count=3 asks the model for up to three names: the model's own order is
    # the ranking and each name is resolved to MusicBrainz's spelling.
    assert got == ["post-britpop", "shoegaze"], got
    # The prompt names the release, the candidates and the shape asked for.
    assert "Radiohead" in prompt and "OK Computer" in prompt, prompt
    assert "Paranoid Android" in prompt, prompt
    assert "shoegaze" in prompt and "dream pop" in prompt, prompt
    assert "AT MOST 3 SPECIFIC" in prompt, prompt
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

    # A broad name the model answers with is a PUBLISHED genre like any other
    # now: nothing above a name is derived or dropped (2026-10-12), so it is
    # kept, in the model's own order, and simply capped. The prompt still asks
    # for specifics, but the answer is not policed for breadth.
    got, _ = ask(FakeChat('{"genres": ["Rock", "Shoegaze"]}'), count=2)
    assert got == ["rock", "shoegaze"], got
    got, _ = ask(FakeChat('{"genres": ["Rock"]}'), count=2)
    assert got == ["rock"], got

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

    # count=1 asks for ONE specific genre — every slot is a genre now, where
    # the old model kept the first slot for the derived family and never
    # called at all — and the answer comes back as the vocabulary spells it.
    ai_mod.ai_chat = FakeChat('{"genres": ["Shoegaze"]}')
    genre_ai._config = lambda: dict(CFG)
    assert genre_ai.infer_genres(artist="A", album="B", count=1) == ["shoegaze"]
    assert len(ai_mod.ai_chat.calls) == 1, ai_mod.ai_chat.calls

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
# 5) trim_genres: the duplicate and the spelling go, the source's ORDER stays
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


# A case-insensitive duplicate goes; the surviving names keep the order they
# were stored in — never re-sorted — and the overflow is trimmed too.
af = FakeAudioFile(["shoegaze", "shoegaze", "dream pop"])
removed = autotag.trim_genres(af, 2)
assert removed == 1, removed
assert af.values == ["Shoegaze", "Dream Pop"], af.values
assert af.written == ["Shoegaze", "Dream Pop"], af.written

# In shape but lowercase: nothing is REMOVED, and what this pass writes is the
# display spelling (mlo.genres.display_name) in the file's own order — the
# order is the source's rank now (2026-10-12) and is never casefold-sorted. A
# container write happens once per file, never per run.
af = FakeAudioFile(["shoegaze", "idm"])
assert autotag.trim_genres(af, 2) == 0
assert af.written == ["Shoegaze", "IDM"], af.written

# Already in shape (display spelling, no repeat): nothing is removed and the
# container is never rewritten.
af = FakeAudioFile(["IDM", "Shoegaze"])
assert autotag.trim_genres(af, 2) == 0
assert af.written is _MISSING, af.written

# …and the SAME names in another order are just as in shape: there is no
# canonical order any more, so the file is left alone rather than re-sorted.
af = FakeAudioFile(["Shoegaze", "IDM"])
assert autotag.trim_genres(af, 2) == 0
assert af.written is _MISSING, af.written

# Over the cap with no duplicates: the overflow goes (three names in, two
# stored, one value removed) and the FIRST N survive untouched.
af = FakeAudioFile(["shoegaze", "dream pop", "post-britpop"])
assert autotag.trim_genres(af, 2) == 1
assert af.values == ["Shoegaze", "Dream Pop"], af.values

# A parent stated beside its own child is left alone and counted like any other
# value now: nothing above a name is derived or dropped, so three in-shape
# names are only re-rendered, never trimmed.
af = FakeAudioFile(["rock", "shoegaze", "dream pop"])
assert autotag.trim_genres(af, 3) == 0
assert af.values == ["Rock", "Shoegaze", "Dream Pop"], af.values
# …and a head a source really stated, in the source's order, is not rewritten.
af = FakeAudioFile(["Rock", "Jazz"])
assert autotag.trim_genres(af, 3) == 0
assert af.written is _MISSING, af.written

# One stored value holding a joined list is split — the file another tagger
# wrote comes out as the names it holds, one field each, in its own order.
af = FakeAudioFile(["Shoegaze; IDM"])
assert autotag.trim_genres(af, 2) == 0     # two names in, two names out
assert af.values == ["Shoegaze", "IDM"], af.values
# Under the cap, one joined value is split into the names it holds and the
# display form is written back: the count is unchanged (nothing removed), the
# stored form is now the app's own.
af = FakeAudioFile(["shoegaze; idm"])
assert autotag.trim_genres(af, 5) == 0
assert af.written == ["Shoegaze", "IDM"], af.written

# count=0 still means "keep none" (an empty GENRE is worse than no GENRE).
af = FakeAudioFile(["shoegaze", "idm"])
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
# 7) genre_chain asks the AI only as a TIE-BREAKER, and replaces the merged
#    list when it answers
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
    # (a) The sources cannot fill the track's three slots (one album-level
    # specific genre), so the model IS asked, with the sources' own names as
    # its candidates.
    intg._genre_source_answers = lambda *a, **k: {
        intg._ALL_TRACKS: {"level": "album", "genres": ["shoegaze"]}}
    seen = []

    def fake_infer(*, artist, album, title="", track_path="", candidates=None,
                   count=3, extra=None):
        seen.append((artist, album, title, list(candidates or []), count, extra))
        # In the model's own casing and order (and with a blank and a repeat
        # on purpose): the chain canonicalizes them, keeping that order and
        # capitalizing for the tag. Nothing is appended.
        return ["Post-Britpop", "shoegaze", "", "shoegaze"]

    genre_ai.infer_genres = fake_infer
    on = intg.genre_chain(artist="Test Artist", album="Test Album",
                          release=RELEASE, files=[], cfg=dict(ON))
    # The model's answer REPLACES the merged list (it is the tie-breaker), in
    # the model's own order, canonicalized.
    assert on["per_track"][(1, 1)] == ["Post-Britpop", "Shoegaze"], on["per_track"]
    assert on["genres"] == ["Post-Britpop", "Shoegaze"], on["genres"]
    assert on["per_track_sources"][(1, 1)] == ["musicbrainz", "ai"], on["per_track_sources"]
    # The ranker was asked with the release identity, the sources' own
    # candidates, the cap and the release context.
    assert seen and seen[0][:2] == ("Test Artist", "Test Album"), seen
    assert seen[0][2] == "Track One", seen
    assert seen[0][3] == ["shoegaze"], seen
    assert seen[0][4] == 3, seen
    assert seen[0][5].get("year") == "1997", seen
    assert seen[0][5].get("country") == "GB", seen

    # (b) The sources ALREADY settle the track — three specifics fill the cap
    # of three — so no model call is spent on repeating them, and the
    # provenance names the sources only.
    seen.clear()
    intg._genre_source_answers = lambda *a, **k: {
        intg._ALL_TRACKS: {"level": "album",
                           "genres": ["shoegaze", "dream pop", "post-britpop"]}}
    settled = intg.genre_chain(artist="Test Artist", album="Test Album",
                              release=RELEASE, files=[], cfg=dict(ON))
    assert seen == [], seen
    # `per_track` carries the merged names in the sources' own spelling — the
    # ONE writer capitalizes (mlo.genres.display_name); this asserts the merge,
    # not the tag.
    assert settled["per_track"][(1, 1)] == ["shoegaze", "dream pop",
                                            "post-britpop"], settled["per_track"]
    assert settled["per_track_sources"][(1, 1)] == ["musicbrainz"], settled["per_track_sources"]

    # The setting off: the ranker is not reached at all, and every field is
    # what the sources alone produced (the merge keeps their spelling — the
    # WRITERS canonicalize).
    intg._genre_source_answers = lambda *a, **k: {
        intg._ALL_TRACKS: {"level": "album", "genres": ["shoegaze"]}}
    seen.clear()
    quiet = intg.genre_chain(artist="Test Artist", album="Test Album",
                             release=RELEASE, files=[],
                             cfg={"mb_genre_count": 3,
                                  "genre_sources": ["musicbrainz"]})
    assert seen == [], seen
    assert quiet["per_track"][(1, 1)] == ["shoegaze"], quiet["per_track"]
    assert quiet["per_track_sources"][(1, 1)] == ["musicbrainz"], quiet["per_track_sources"]

    # A ranker that raises is swallowed whole: a broken endpoint must never
    # fail the import it feeds.
    def dead_infer(**kwargs):
        raise RuntimeError("no endpoint")

    genre_ai.infer_genres = dead_infer
    off = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=RELEASE, files=[], cfg=dict(ON))
    assert off["per_track"][(1, 1)] == ["shoegaze"], off["per_track"]
    assert off["per_track_sources"][(1, 1)] == ["musicbrainz"], off["per_track_sources"]
    assert off["genres"] == ["shoegaze"], off["genres"]
finally:
    intg._genre_source_answers = _real_sources
    genre_ai.infer_genres = _real_infer

print("genre format: all assertions passed")
sys.exit(0)
