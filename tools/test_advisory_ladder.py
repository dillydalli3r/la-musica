"""ITUNESADVISORY when no provider states one — the ladder, and the genre rule.

Issue #10, in one file:

* the merge rule is 1 > 0 > 2 and "nobody stated anything" is None, never an
  invented value (`integrations.merge_advisory`, exercised in
  `test_advisory_sources.py`);
* when nobody stated anything, `mlo.advisory.decide_advisory` runs the ladder:
  instrumental → AI (fed the lyrics when the file has them) → lyrics word scan
  → `advisory_fallback`, and every answer carries the stage that produced it;
* a provider's stated 0 is ESCALATEABLE: the two stages that read the words
  run as explicit-only signals, and one of them finding explicit language
  turns the 0 into 1 with a source that names it ("lyrics-scan (escalated)",
  "ai-lyrics (escalated)") instead of leaving a provider credited with a
  rating it never gave. A stated 1 or 2 is final — escalation only ever goes
  0 → 1, and a track with no words cannot be escalated at all;
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
# 1) A provider that stated something wins outright: no AI call, no scan.
# --------------------------------------------------------------------------- #
calls = []
ai_mod.ai_configured = lambda c: True
ai_mod.ai_chat = lambda *a, **k: calls.append(k) or "1"
try:
    out = advisory.decide_advisory(cfg(), value=1, source="deezer-isrc",
                                   lyrics="i dont give a fuck")
    assert out == {"value": 1, "source": "deezer-isrc", "stage": "sources",
                   "hits": [], "fallback": False}, out
    assert calls == [], calls

    # a provider that stated 2 (a clean edition) is equally final
    out = advisory.decide_advisory(cfg(), value=2, source="apple-album", lyrics="")
    assert out["value"] == 2 and out["stage"] == "sources", out

    # ----------------------------------------------------------------------- #
    # 1b) A stated 0 is not: the stages that read the words run as EXPLICIT-
    #     only signals, and the source names the one that overruled the
    #     provider. The providers miss exactly here — Deezer's
    #     `explicit_lyrics: false` also means "not classified", Apple's
    #     `notExplicit` is the master's own flag — so a 0 that the words
    #     contradict must not be the end of the question.
    # ----------------------------------------------------------------------- #
    calls.clear()
    out = advisory.decide_advisory(cfg(), value=0, source="apple-album",
                                   lyrics="i dont give a fuck")
    assert out == {"value": 1, "source": "ai-lyrics (escalated)",
                   "stage": "ai", "hits": [], "fallback": False}, out
    assert calls, "the AI read the words and is what escalated the 0"

    # no AI: the word scan is the signal, and the answer carries both things a
    # reader needs — the terms it hit and the fact the provider disagreed
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

    # a clean edition's 2 is final too: the words in the FILE may be the
    # edited ones, and 2 is the provider's own statement about the edition
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
    assert "0 = no explicit content" in seen["system"], seen
    assert "3 = cannot tell" in seen["system"], seen

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
    # addition to the word list, never a requirement for it)
    ai_mod.ai_configured = lambda c: True
    out = advisory.decide_advisory(cfg(advisory_ai_classify=False),
                                   lyrics="i dont give a fuck")
    assert out["stage"] == "lyrics" and out["value"] == 1, out

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
