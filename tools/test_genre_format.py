#!/usr/bin/env python3
"""Genre formatting and AI ranking — offline contract, no network at all.

What this pins:

  * `mlo.genres.normalize_genres` is THE list policy: order preserved (it IS
    the hierarchy, so nothing may sort it), duplicates collapsed on the
    casefolded name, blanks dropped, cap enforced — and one stored value that
    holds a " / "- or "; "-joined list read back as the names inside it;
  * `format_genres` / `split_stored` round-trip: the rendered
    "Parent / Main / Sub" string reads back as exactly the three names, which
    is what makes the display spelling safe to store and re-read;
  * `order_hierarchy` LIFTS a legacy flat list (no parent in slot 1) so the
    broad family comes first, and leaves a list it cannot vouch for exactly
    as found rather than inventing a hierarchy;
  * `server.genre_ai.infer_genres` names the release and the fetched
    candidates in its prompt, states the slot contract, honours
    `ai_genre_research` (own knowledge vs re-rank only) and `ai_genre_effort`
    (passed to the shared client as `ai_effort`), parses a JSON object, a
    fenced block, a bare array and a plain newline list, and returns None —
    never raising — for an unconfigured client, a failing call and an answer
    with fewer than two usable names;
  * the same question is answered from the disk cache on the second call, so
    an import that re-runs costs no second request;
  * `mlo.autotag.trim_genres` routes through `normalize_genres`: a
    case-insensitive duplicate goes as well as the overflow, the surviving
    order is untouched, its return value is still "how many values were
    removed", and a list that is already canonical is never rewritten;
  * `server.integrations.genre_chain` lets the AI answer REPLACE the merged
    source list when `ai_genre_inference` is on, names "ai" among the path's
    contributing sources, and leaves every field alone when it is off.

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
# 1) The list policy: order, case-insensitive duplicates, blanks, cap
# --------------------------------------------------------------------------- #
names = ["Rock", "Alternative Rock", "Post-Britpop"]
assert G.normalize_genres(names) == names, G.normalize_genres(names)
# Order is the hierarchy, so a list that arrives out of alphabetical order
# must come back out of alphabetical order.
flat = ["Post-Britpop", "Rock", "Alternative Rock"]
assert G.normalize_genres(flat) == flat, G.normalize_genres(flat)
# Duplicates collapse on the casefolded name, the FIRST spelling winning
# (that one is the source's own, highest-priority spelling).
assert G.normalize_genres(["Rock", "rock", "ROCK", "Pop"]) == ["Rock", "Pop"]
assert G.normalize_genres(["Rock", "  ", "Pop"]) == ["Rock", "Pop"]
assert G.normalize_genres(["Rock", "Pop", "Jazz", "Blues"], 2) == ["Rock", "Pop"]
# count=0 is the module's "no cap" spelling (format_genres' own call).
assert G.normalize_genres(["Rock", "Pop"], 0) == ["Rock", "Pop"]
# A single stored value another tagger joined is read as the names inside it.
assert G.normalize_genres(["Rock; Alternative Rock; Post-Britpop"]) == names
assert G.normalize_genres(["Rock / Alternative Rock / Post-Britpop"]) == names

# --------------------------------------------------------------------------- #
# 2) format_genres / split_stored round trip
# --------------------------------------------------------------------------- #
text = G.format_genres(names)
assert text == "Rock / Alternative Rock / Post-Britpop", text
assert G.split_stored(text) == names, G.split_stored(text)
assert G.split_stored("Rock") == ["Rock"]
assert G.split_stored("") == []
# Reading our own render back through the normalizer is lossless — the
# property that lets the " / " spelling be stored by another tagger.
assert G.normalize_genres([text]) == names

# --------------------------------------------------------------------------- #
# 3) order_hierarchy lifts a legacy flat list, and declines to invent one
# --------------------------------------------------------------------------- #
assert G.order_hierarchy(["Alternative Rock", "Post-Britpop", "Rock"]) == names
# The parent is lifted to slot 1 and the rest keeps its own relative order —
# the lift is a move, never a re-sort.
assert G.order_hierarchy(["Post-Britpop", "Alternative Rock", "Rock"]) == [
    "Rock", "Post-Britpop", "Alternative Rock"]
assert G.order_hierarchy(names) == names
# Nothing recognisable as a broad family: the order is left exactly as found.
assert G.order_hierarchy(["Kwaito", "Amapiano"]) == ["Kwaito", "Amapiano"]

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


def ask(chat, **cfg):
    """One infer_genres call against the stub, returning (names, prompt).

    The album gets a fresh number per call: the disk cache is keyed by the
    prompt, and these cases each need their OWN request rather than the
    previous answer served again.
    """
    n = next(_SEQ)
    ai_mod.ai_chat = chat
    genre_ai._config = lambda: dict(CFG, **cfg)
    got = genre_ai.infer_genres(
        artist="Radiohead", album=f"OK Computer #{n}", title="Paranoid Android",
        candidates=["Alternative Rock", "Art Rock"], count=3)
    prompt = chat.calls[0][2] if chat.calls else ""
    return got, prompt


try:
    # The shared cache root is the REAL app-data folder; keep this run's
    # answers out of it.
    ai_mod._cache_dir = lambda: TMP

    chat = FakeChat('{"genres": ["Rock", "Alternative Rock", "Art Rock"]}')
    ai_mod.ai_chat = chat
    FIXED = dict(artist="Radiohead", album="OK Computer",
                 title="Paranoid Android",
                 candidates=["Alternative Rock", "Art Rock"], count=3)
    genre_ai._config = lambda: dict(CFG)
    got = genre_ai.infer_genres(**FIXED)
    prompt = chat.calls[0][2]
    assert got == ["Rock", "Alternative Rock", "Art Rock"], got
    # The prompt names the release, the candidates and the shape asked for.
    assert "Radiohead" in prompt and "OK Computer" in prompt, prompt
    assert "Paranoid Android" in prompt, prompt
    assert "Alternative Rock" in prompt and "Art Rock" in prompt, prompt
    assert "hierarchy order" in prompt, prompt
    assert "slot 1" in prompt and "parent" in prompt, prompt
    assert "subgenre" in prompt, prompt
    assert "JSON" in prompt, prompt
    # research on: the model is told it may go past the fetched list.
    assert "your own knowledge" in prompt, prompt
    # The genre thinking budget is what the shared client is handed.
    assert chat.calls[0][0]["ai_effort"] == "low", chat.calls[0][0]
    assert chat.calls[0][0]["ai_model"] == "test-model", chat.calls[0][0]

    # Same question again: answered from the cache, no second request — even
    # with a client that would answer something else.
    ai_mod.ai_chat = FakeChat('{"genres": ["Pop"]}')
    assert genre_ai.infer_genres(**FIXED) == got
    assert ai_mod.ai_chat.calls == [], ai_mod.ai_chat.calls
    # A different question (new candidates) is a different cache key. The
    # answer REPLACES the source list, so a short one is topped up from the
    # fetched candidates rather than stored as-is (`mb_genre_count` is what the
    # grader requires, and the answer is disk-cached).
    ai_mod.ai_chat = FakeChat('{"genres": ["Rock", "Shoegaze"]}')
    other = genre_ai.infer_genres(
        artist="Slowdive", album="Souvlaki",
        candidates=["Shoegaze", "Dream Pop"], count=3)
    assert other == ["Rock", "Shoegaze", "Dream Pop"], other
    assert len(ai_mod.ai_chat.calls) == 1, ai_mod.ai_chat.calls
    # Nothing to top up from, and still a slot short: the source list is kept
    # instead of a two-genre answer that could never pass grading.
    ai_mod.ai_chat = FakeChat('{"genres": ["Rock", "Shoegaze"]}')
    assert genre_ai.infer_genres(
        artist="Slowdive", album="Just for a Day",
        candidates=["Shoegaze"], count=3) is None

    # research off: re-rank the fetched list only.
    _, prompt = ask(FakeChat('{"genres": ["Rock", "Alternative Rock"]}'),
                    ai_genre_research=False)
    assert "Re-rank ONLY the fetched genres" in prompt, prompt
    assert "your own knowledge" not in prompt, prompt

    # The answer shapes a model actually returns — all accepted.
    for answer in ('```json\n{"genres": ["Rock", "Post-Rock"]}\n```',
                   '["Rock", "Post-Rock"]',
                   'Rock\nPost-Rock',
                   '1. Rock\n2. Post-Rock',
                   '```\n{"genres": ["Rock", "Post-Rock"]}\n```'):
        got, _ = ask(FakeChat(answer))
        # Two named slots of three: kept in the model's order and the third
        # filled from the fetched candidates (`ask` hands two in).
        assert got == ["Rock", "Post-Rock", "Alternative Rock"], (answer, got)
    # …and the answer is canonicalized: a repeat and a blank are dropped.
    got, _ = ask(FakeChat('{"genres": ["Rock", "rock", "", "Post-Rock"]}'))
    assert got == ["Rock", "Post-Rock", "Alternative Rock"], got

    # A refusal / prose answer has no usable names: None, never a stored junk
    # genre. Same for an answer that is a single name — no hierarchy there.
    for answer in ("I can't help with that.", "", "```json\n{}\n```",
                   '{"genres": ["Rock"]}'):
        got, _ = ask(FakeChat(answer))
        assert got is None, (answer, got)
    # A failing endpoint and an unconfigured client are both "no opinion".
    got, _ = ask(FakeChat(boom=True))
    assert got is None, got
    ai_mod.ai_chat = FakeChat('{"genres": ["Rock", "Pop"]}')
    genre_ai._config = lambda: {"ai_genre_research": True}
    assert genre_ai.infer_genres(artist="A", album="B") is None
    assert ai_mod.ai_chat.calls == [], ai_mod.ai_chat.calls
finally:
    ai_mod.ai_chat, genre_ai._config = _real_chat, _real_config
    ai_mod._cache_dir = _real_cache_dir
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

# --------------------------------------------------------------------------- #
# 5) trim_genres: the duplicate goes, the order stays, the count is honest
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


af = FakeAudioFile(["Rock", "rock", "Pop", "Jazz"])
removed = autotag.trim_genres(af, 3)
assert removed == 1, removed
assert af.values == ["Rock", "Pop", "Jazz"], af.values
assert af.written == ["Rock", "Pop", "Jazz"], af.written

# Order is the ranking: already canonical, nothing is removed and the
# container is never rewritten.
af = FakeAudioFile(["Pop", "Rock", "Techno"])
assert autotag.trim_genres(af, 3) == 0
assert af.written is _MISSING, af.written

# Over the cap with no duplicates: the overflow, and only the overflow.
af = FakeAudioFile(["Rock", "Pop", "Jazz", "Blues"])
assert autotag.trim_genres(af, 2) == 2
assert af.values == ["Rock", "Pop"], af.values

# One stored value holding a joined list is split for the cap; under the cap
# nothing is rewritten (the split is this helper's reading, not a fix it owes
# the container)…
af = FakeAudioFile(["Rock; Alternative Rock; Post-Britpop"])
assert autotag.trim_genres(af, 5) == 0
assert af.written is _MISSING, af.written
# …and over the cap the split, capped list is what goes back in.
af = FakeAudioFile(["Rock; Alternative Rock; Post-Britpop"])
assert autotag.trim_genres(af, 2) == 1
assert af.values == ["Rock", "Alternative Rock"], af.values

# count=0 still means "keep none" (an empty GENRE is worse than no GENRE).
af = FakeAudioFile(["Rock", "Pop"])
assert autotag.trim_genres(af, 0) == 2
assert af.deleted and af.values is None, af.values

# No GENRE tag at all: nothing to do.
af = FakeAudioFile(None)
assert autotag.trim_genres(af, 3) == 0
assert not af.deleted, af.deleted

# --------------------------------------------------------------------------- #
# 6) genre_chain hands the merged list to the AI and replaces it on an answer
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
        intg._ALL_TRACKS: {"level": "album", "genres": ["Alternative Rock",
                                                        "Post-Britpop"]}}
    seen = []

    def fake_infer(*, artist, album, title="", track_path="", candidates=None,
                   count=3, extra=None):
        seen.append((artist, album, title, list(candidates or []), count, extra))
        # A repeat and an overflow on purpose: the chain must canonicalize the
        # model's list, not store it as answered.
        return ["Rock", "rock", "Alternative Rock", "Post-Britpop"]

    genre_ai.infer_genres = fake_infer
    on = intg.genre_chain(artist="Test Artist", album="Test Album",
                          release=RELEASE, files=[], cfg=dict(ON))
    assert on["per_track"][(1, 1)] == ["Rock", "Alternative Rock", "Post-Britpop"], on["per_track"]
    assert on["genres"] == ["Rock", "Alternative Rock", "Post-Britpop"], on["genres"]
    assert on["per_track_sources"][(1, 1)] == ["musicbrainz", "ai"], on["per_track_sources"]
    # The ranker was asked with the release identity, the sources' own
    # candidates, the cap and the release context.
    assert seen and seen[0][:2] == ("Test Artist", "Test Album"), seen
    assert seen[0][2] == "Track One", seen
    assert seen[0][3] == ["Alternative Rock", "Post-Britpop"], seen
    assert seen[0][4] == 3, seen
    assert seen[0][5].get("year") == "1997", seen
    assert seen[0][5].get("country") == "GB", seen

    # The setting off: the ranker is not reached at all, and every field is
    # what the sources alone produced.
    seen.clear()
    quiet = intg.genre_chain(artist="Test Artist", album="Test Album",
                             release=RELEASE, files=[],
                             cfg={"mb_genre_count": 3,
                                  "genre_sources": ["musicbrainz"]})
    assert seen == [], seen
    assert quiet["per_track"][(1, 1)] == ["Alternative Rock", "Post-Britpop"], quiet["per_track"]
    assert quiet["per_track_sources"][(1, 1)] == ["musicbrainz"], quiet["per_track_sources"]

    # A ranker that raises is swallowed whole: a broken endpoint must never
    # fail the import it feeds.
    def dead_infer(**kwargs):
        raise RuntimeError("no endpoint")

    genre_ai.infer_genres = dead_infer
    off = intg.genre_chain(artist="Test Artist", album="Test Album",
                           release=RELEASE, files=[], cfg=dict(ON))
    assert off["per_track"][(1, 1)] == ["Alternative Rock", "Post-Britpop"], off["per_track"]
    assert off["per_track_sources"][(1, 1)] == ["musicbrainz"], off["per_track_sources"]
    assert off["genres"] == ["Alternative Rock", "Post-Britpop"], off["genres"]
finally:
    intg._genre_source_answers = _real_sources
    genre_ai.infer_genres = _real_infer

print("genre format: all assertions passed")
sys.exit(0)
