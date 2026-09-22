"""ITUNESADVISORY when no provider states one — the ladder, and the genre rule.

Issue #10, in one file:

* the merge rule is 1 > 0 > 2 and "nobody stated anything" is None, never an
  invented value (`integrations.merge_advisory`, exercised in
  `test_advisory_sources.py`);
* the AI provider is a SOURCE in that merge (issue #28): it is asked ONCE for
  every track that reaches `mlo.advisory.decide_advisory` whenever one is
  configured and `advisory_ai_classify` is on — whether or not the network
  sources stated a value — and its answer is ranked against theirs by the same
  1 > 0 > 2 rule. A stated 1 survives it, an AI 1 outranks a stated 0 or 2,
  and an AI 2 never outranks a stated 0. Its answer is recorded in the caller's
  per-source map either way, so the reply can name it;
* it outranks a stated value only when it READ the track's words: a model shown
  "(none available)" has read nothing, and nothing is not the evidence it takes
  to contradict a provider. With no AI answering, nothing is asked of anyone
  and the ladder below behaves exactly as it always did;
* when nobody stated anything, the ladder runs: instrumental → AI (fed the
  lyrics when the file has them) → lyrics word scan → `advisory_fallback`, and
  every answer carries the stage that produced it;
* a provider's stated 0 is ESCALATEABLE: the stages that read the words run as
  explicit-only signals, and one of them finding explicit language turns the 0
  into 1 with a source that names it ("lyrics-scan (escalated)", "ai-lyrics
  (escalated)") instead of leaving a provider credited with a rating it never
  gave. The SCAN only ever goes 0 → 1, and a track with no words cannot be
  escalated at all;
* an AI that answers 3 (or something unparseable) is "cannot tell" and falls
  THROUGH the ladder — the value space this app stores is 0/1/2, so a stored 3
  would fail the grader on every track carrying one;
* `advisory_fallback` is the user's last resort: "0", "2" or "none" (None
  means leave the tag alone);
* a track with no lyrics is never read as clean by the SCAN — only the
  fallback decides those;
* script 8 imports no genre and invents none: `mlo.autotag` has no provider
  hook left, and trimming a track that carries no GENRE writes nothing.

Run:  python tools/test_advisory_ladder.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import advisory
from mlo import advisory_words
from mlo import autotag
from server import ai as ai_mod

_MISSING = object()


class FakeAudioFile:
    """`get_tag` for the ladder, plus the three methods `trim_genres` uses."""

    def __init__(self, tags=None, values=None):
        self.tags = dict(tags or {})
        self.values = values
        self.written = _MISSING

    def get_tag(self, name):
        return self.tags.get(name)

    def get_lyrics(self):
        """Empty: the embedded LYRICS tag holds nothing, which is what makes
        `local_lyrics` fall through to the .lrc sidecar."""
        return None

    def tag_values(self, name):
        return [] if self.values is None else list(self.values)

    def set_tag(self, name, value):
        self.values = list(value) if isinstance(value, list) else [value]
        self.written = value
        return True

    def delete_tag(self, name):
        self.values, self.written = None, []
        return True


def cfg(**over):
    base = {"advisory_ai_classify": True, "advisory_lyrics_scan": True,
            "advisory_fallback": "0", "auto_zero_advisory_for_instrumental": True,
            "ai_base_url": "http://127.0.0.1:9/v1", "ai_model": "test-model"}
    base.update(over)
    return base


_real_configured, _real_chat = ai_mod.ai_configured, ai_mod.ai_chat

# --------------------------------------------------------------------------- #
# 1) The AI is asked for EVERY track — issue #28: it is a source now, not a
#    last resort — and the app's one rank (1 beats 0 beats 2) decides between
#    its answer and the providers'. A stated 1 survives it outright; a stated 0
#    does not. Each case counts the calls: the ask is unconditional, and it is
#    made ONCE per track however many stages want the answer.
# --------------------------------------------------------------------------- #
calls = []
ai_mod.ai_configured = lambda c: True
ai_mod.ai_chat = lambda *a, **k: calls.append(k) or "1"
try:
    out = advisory.decide_advisory(cfg(), value=1, source="deezer-isrc",
                                   lyrics="i dont give a fuck")
    assert out == {"value": 1, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out
    assert len(calls) == 1, (
        f"the AI WAS asked about a track a provider already rated ({calls}) — "
        "and its 1 cannot outrank the provider's own 1, so the value AND the "
        "provenance stay the provider's")

    # the answer it gave is recorded beside the providers', which is what lets
    # the reply's provenance chips name it (the fetch's per-track `answers`)
    track_answers = {}
    advisory.decide_advisory(cfg(), value=1, source="deezer-isrc",
                             answers=track_answers, lyrics="i dont give a fuck")
    assert track_answers == {"ai-lyrics": 1}, track_answers

    # a stated 2 with NO words to read: the AI's answer cannot outrank it — a
    # model shown "(none available)" has read nothing, and nothing is not the
    # evidence it takes to contradict a provider
    out = advisory.decide_advisory(cfg(), value=2, source="apple-album", lyrics="")
    assert out == {"value": 2, "source": "apple-album", "stage": "sources",
                   "hits": [], "fallback": False}, out

    # ----------------------------------------------------------------------- #
    # 1b) A stated 0 is not final: an AI that READ THE WORDS and found them
    #     explicit outranks it. The providers miss exactly here — Deezer's
    #     `explicit_lyrics: false` also means "not classified", Apple's
    #     `notExplicit` is the master's own flag — so a 0 that the words
    #     contradict must not be the end of the question. The source names the
    #     stage that overruled the provider, and the escalation costs no second
    #     call: it reuses the answer the ladder's own step would have used.
    # ----------------------------------------------------------------------- #
    calls.clear()
    out = advisory.decide_advisory(cfg(), value=0, source="apple-album",
                                   lyrics="i dont give a fuck")
    assert out == {"value": 1, "source": "ai-lyrics (escalated)",
                   "stage": "ai", "hits": [], "fallback": False}, out
    assert len(calls) == 1, f"ONE call per track, not one per stage ({calls})"

    # no AI: the word scan is the signal, and the answer carries both things a
    # reader needs — the terms it hit and the fact the provider disagreed
    calls.clear()
    ai_mod.ai_configured = lambda c: False
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc",
                                   lyrics="shut the fuck up")
    assert out["value"] == 1 and out["stage"] == "lyrics", out
    assert out["source"] == "lyrics-scan (escalated)", out
    assert out["hits"] == ["fuck"], out

    # a stated 0 the words AGREE with stays the provider's answer: no
    # escalation and no invented source
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc",
                                   lyrics="we built this city on rock and roll")
    assert out == {"value": 0, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out

    # a track with no words cannot be escalated at all: there is nothing to
    # read, and nothing is not the evidence it takes to contradict a provider
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc", lyrics="")
    assert out == {"value": 0, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out
    assert calls == [], "no AI configured: nothing is asked, at any stage"

    # a clean edition's 2 is final when nothing READ the words: the file may be
    # the edited one, and 2 is the provider's own statement about the edition
    out = advisory.decide_advisory(cfg(), value=2, source="apple-album",
                                   lyrics="i dont give a fuck")
    assert out["value"] == 2 and out["stage"] == "sources", out

    # ... and an INSTRUMENTAL track's 0 is not escalateable either: it has no
    # words of its own, so a text file sitting beside it is not evidence about
    # it (the ladder's own first rung, which outranks any read of the words)
    af_inst = FakeAudioFile({"INSTRUMENTAL": "1"})
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc",
                                   af=af_inst, lyrics="i dont give a fuck")
    assert out == {"value": 0, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out
    # with the instrumental rule off the words ARE read, so the 0 escalates
    out = advisory.decide_advisory(
        cfg(auto_zero_advisory_for_instrumental=False), value=0,
        source="deezer-isrc", af=af_inst, lyrics="i dont give a fuck")
    assert out["value"] == 1 and out["source"] == "lyrics-scan (escalated)", out
    assert out["hits"] == ["fuck"], out

    # an AI that answers "cannot tell" has said nothing: the scan still runs
    # and is what answers a stated 0
    ai_mod.ai_configured = lambda c: True
    ai_mod.ai_chat = lambda *a, **k: "3"      # cannot tell
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc",
                                   lyrics="shut the fuck up")
    assert out["source"] == "lyrics-scan (escalated)" and out["value"] == 1, out

    # the AI answering 0 IS an answer — it agrees with the provider and the
    # word list is not consulted over it (the ladder's own order: a model
    # reading the words replaces the word list, it does not back it up)
    ai_mod.ai_chat = lambda *a, **k: "0"
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc",
                                   lyrics="i dont give a fuck")
    assert out == {"value": 0, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out
    assert advisory_words.scan_lyrics("i dont give a fuck"), \
        "the lexicon WOULD have hit — the AI's answer is what settled it"

    # an AI 2 never outranks a stated 0 — that is the SAME rank, read the other
    # way — and because the AI's answer replaces the word list, the scan is not
    # consulted over it either
    calls.clear()
    ai_mod.ai_chat = lambda *a, **k: calls.append(k) or "2"
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc",
                                   lyrics="i dont give a fuck")
    assert out == {"value": 0, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out
    assert len(calls) == 1, calls

    # ... while an AI 1 DOES outrank a clean edition's 2: as a source in the
    # merge the rank decides, and the provenance is the AI's own name. The
    # "(escalated)" wording the UI renders is for the case the providers
    # systematically miss — a provider that stated 0.
    ai_mod.ai_chat = lambda *a, **k: "1"
    out = advisory.decide_advisory(cfg(), value=2, source="apple-album",
                                   lyrics="i dont give a fuck")
    assert out == {"value": 1, "source": "ai-lyrics", "stage": "ai",
                   "hits": [], "fallback": False}, out

    # without a read of the words it outranks nothing, so a stated 0 still
    # stands: the call is made and its answer reported, but the provider's
    # value decides
    track_answers = {}
    out = advisory.decide_advisory(cfg(), value=0, source="deezer-isrc",
                                   answers=track_answers, lyrics="")
    assert out == {"value": 0, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out
    assert track_answers == {"ai": 1}, track_answers

    ai_mod.ai_chat = lambda *a, **k: calls.append(k) or "1"
    # the sections below measure the AI calls their OWN case makes
    calls.clear()

    # ----------------------------------------------------------------------- #
    # 2) An instrumental has no words to be explicit with → 0, before the AI.
    # ----------------------------------------------------------------------- #
    af = FakeAudioFile({"INSTRUMENTAL": "1"})
    out = advisory.decide_advisory(cfg(), af=af, lyrics="i dont give a fuck")
    assert out["value"] == 0 and out["stage"] == "instrumental", out
    assert calls == [], "the AI is not asked about an instrumental"

    # the setting off means the rule does not fire — the ladder continues
    out = advisory.decide_advisory(
        cfg(auto_zero_advisory_for_instrumental=False), af=af, lyrics="")
    assert out["stage"] != "instrumental", out

    # ----------------------------------------------------------------------- #
    # 3) The AI reads the lyrics and is asked BEFORE the word list.
    # ----------------------------------------------------------------------- #
    seen = {}

    def fake_chat(cfg_, system, user, timeout=None):
        seen["system"], seen["user"] = system, user
        return "Answer: 1"

    ai_mod.ai_chat = fake_chat
    out = advisory.decide_advisory(
        cfg(),
        af=FakeAudioFile({"ARTIST": "Rihanna", "TITLE": "S&M", "ALBUM": "Loud"}),
        lyrics="sticks and stones may break my bones")
    assert out["value"] == 1 and out["source"] == "ai-lyrics", out
    assert out["stage"] == "ai" and out["hits"] == [], out
    assert "sticks and stones may break my bones" in seen["user"], seen
    assert "Rihanna" in seen["user"] and "S&M" in seen["user"], seen
    # The prompt IS the rubric, and the parser only accepts 0/1/2 (3 falls
    # through) — so what the prompt must say is that the four answers exist and
    # that mild language alone is not explicit. Assert those two facts, not a
    # sentence: the wording is the model's instruction, not this suite's
    # contract.
    rubric = seen["system"].lower()
    assert all(f"{digit} =" in rubric for digit in "0123"), seen
    assert "mild" in rubric and "explicit" in rubric, seen

    # ----------------------------------------------------------------------- #
    # 4) The AI saying 3 (cannot tell) or nonsense falls THROUGH to the scan.
    # ----------------------------------------------------------------------- #
    for reply in ("3", "Answer: 3", "no idea", "", "maybe"):
        ai_mod.ai_chat = lambda *a, _r=reply, **k: _r
        out = advisory.decide_advisory(cfg(), lyrics="i dont give a fuck about you")
        assert out["stage"] == "lyrics", (reply, out)
        assert out["value"] == 1 and out["hits"], (reply, out)
        assert advisory_words.scan_lyrics("i dont give a fuck about you"), \
            "the lexicon must know this line"

    # a scan hit really is what makes it explicit, and a clean line is 0
    out = advisory.decide_advisory(cfg(), lyrics="we built this city on rock and roll")
    assert out["stage"] == "lyrics" and out["value"] == 0 and out["hits"] == [], out

    # ----------------------------------------------------------------------- #
    # 4b) MILD language alone does not make a song explicit. The lexicon's
    #     mild tier ("ass", "arse", "culo") is REPORTED and never decides: the
    #     scan's job is to overrule a provider only when the words say so, and
    #     a passing "ass" says nothing about the song.
    # ----------------------------------------------------------------------- #
    ai_mod.ai_configured = lambda c: False
    for line in ("you're an ass", "shake that ass", "my arse", "un culo"):
        out = advisory.decide_advisory(cfg(), lyrics=line)
        assert out["stage"] == "lyrics" and out["value"] == 0, (line, out)
        assert out["hits"], f"the mild hit is still reported: {line}"
        assert not advisory_words.strong(out["hits"]), (line, out)
        assert advisory_words.MILD, "the mild tier must not be empty"
    # ... and over a provider's stated 0 the mild words agree with it: no
    # escalation, the provider keeps its value and its credit.
    out = advisory.decide_advisory(cfg(), value=0, source="apple-album",
                                   lyrics="shake that ass")
    assert out["value"] == 0 and out["source"] == "apple-album", out
    # One strong word in the same lyric still escalates, tier or no tier.
    out = advisory.decide_advisory(cfg(), value=0, source="apple-album",
                                   lyrics="shake that ass and fuck off")
    assert out["value"] == 1 and out["source"] == "lyrics-scan (escalated)", out
    assert advisory_words.strong(out["hits"]) == ["fuck"], out

    # ----------------------------------------------------------------------- #
    # 5) No AI configured: the scan is the first move, not a second opinion.
    # ----------------------------------------------------------------------- #
    ai_mod.ai_configured = lambda c: False
    out = advisory.decide_advisory(cfg(), lyrics="shut the fuck up")
    assert out["stage"] == "lyrics" and out["value"] == 1, out

    # ----------------------------------------------------------------------- #
    # 6) No lyrics: the scan never reads silence as clean — the fallback does.
    # ----------------------------------------------------------------------- #
    for raw, want in (("0", 0), ("2", 2), ("none", None)):
        out = advisory.decide_advisory(cfg(advisory_fallback=raw), lyrics="")
        assert out["stage"] == "fallback" and out["value"] == want, (raw, out)
        assert out["fallback"] is True, out
    # an unknown value in a hand-edited config is not a crash: it is 0
    out = advisory.decide_advisory(cfg(advisory_fallback="nonsense"), lyrics="")
    assert out["value"] == 0, out

    # the scan switched off: the fallback answers even when lyrics exist
    out = advisory.decide_advisory(cfg(advisory_lyrics_scan=False), lyrics="fuck")
    assert out["stage"] == "fallback" and out["value"] == 0, out

    # the AI switched off with lyrics present: the scan answers (the AI is an
    # addition to the word list, never a requirement for it) and the provider
    # is not spoken to at all — a switched-off feature costs nothing, and the
    # ladder behaves exactly as it did before issue #28
    ai_mod.ai_configured = lambda c: True
    ai_mod.ai_chat = lambda *a, **k: calls.append(k) or "1"
    calls.clear()
    out = advisory.decide_advisory(cfg(advisory_ai_classify=False),
                                   lyrics="i dont give a fuck")
    assert out["stage"] == "lyrics" and out["value"] == 1, out
    assert calls == [], "advisory_ai_classify off: the AI is never asked"
    out = advisory.decide_advisory(cfg(advisory_ai_classify=False), value=0,
                                   source="apple-album",
                                   lyrics="i dont give a fuck")
    assert calls == [], "nor over a provider's stated value"
    assert out["value"] == 1 and out["source"] == "lyrics-scan (escalated)", out

    # ----------------------------------------------------------------------- #
    # 7) The lyrics come off the DISK when the caller has none in hand: the
    #    embedded tag first, then the .lrc sidecar.
    # ----------------------------------------------------------------------- #
    root = tempfile.mkdtemp(prefix="mlo_advisory_ladder_")
    track = os.path.join(root, "01 - Song.flac")
    with open(track, "wb") as fh:
        fh.write(b"")
    with open(os.path.join(root, "01 - Song.lrc"), "w", encoding="utf-8") as fh:
        fh.write("[00:12.34] i dont give a fuck about you\n[00:15.00] and i mean it\n")
    ai_mod.ai_configured = lambda c: False
    out = advisory.decide_advisory(cfg(), path=track, af=FakeAudioFile())
    assert out["stage"] == "lyrics" and out["value"] == 1 and out["hits"], out
    # ... and that same read is what escalates a provider's stated 0: the
    # escalation fetches the words itself, it never demands them from the
    # caller first
    out = advisory.decide_advisory(cfg(), value=0, source="apple-album",
                                   path=track, af=FakeAudioFile())
    assert out["value"] == 1 and out["source"] == "lyrics-scan (escalated)", out
    assert out["hits"] == ["fuck"], out
finally:
    ai_mod.ai_configured, ai_mod.ai_chat = _real_configured, _real_chat

# --------------------------------------------------------------------------- #
# 8) Script 8 imports no genre: no provider hook left, and a track with no
#    GENRE gets none (only the cap is enforced).
# --------------------------------------------------------------------------- #
assert not hasattr(autotag, "set_genre_lookup"), "the genre hook is gone"
assert not hasattr(autotag, "_genre_lookup"), "the genre hook is gone"

fake = FakeAudioFile(values=None)
assert autotag.trim_genres(fake, 2) == 0
assert fake.written is _MISSING, fake.written

print("advisory ladder: all assertions passed")
