#!/usr/bin/env python3
"""Artist description identity (server.discovery.artist_description) — offline.

An artist's Wikipedia article is rarely at their bare name: the exact TITLE
comes from their Wikidata item, reached through the MusicBrainz `wikidata`
relation (or Wikidata's own search with no MBID). What this pins, with
discovery's JSON transport and MusicBrainz stubbed (no network at all):

  * an MBID whose MusicBrainz relations state a Wikidata item is answered by
    the article that item's `enwiki` sitelink names — the WHOLE article
    (`wikipedia_text`), never the article sitting at the artist's bare name;
  * `description_full` off keeps the same resolved title but takes the lead
    paragraph (`wikipedia_summary`);
  * an MBID with NO Wikidata relation falls through to the unchanged order
    (TheAudioDB's own MBID record), so an artist with no Wikidata link keeps
    today's behaviour;
  * a name alone resolves through Wikidata's search first, so an ambiguous
    name ("Nirvana" the band vs the concept) does not land on the concept;
  * `_article_prose` keeps the article's links: wikilinks (relative,
    protocol-relative) become markdown with an ABSOLUTE target, entities are
    decoded, citation `<sup>`s and maintenance hrefs vanish, headings keep
    their wiki shape (`== History ==`, `=== Sub ===`) and sections left with
    no prose lose their headings;
  * a parse request that answers nothing falls back to `prop=extracts`
    (`explaintext`), and one that answers nothing either falls back to the
    REST summary — the description never regresses to no text.

Run:  python tools/test_description_identity.py
"""
import os
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import discovery
from server import integrations as intg

FAILS = []


def check(label, cond, extra=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILS.append(label)
        print(f"       {extra!r}")


MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
QID = "Q11649"
BAND = "Nirvana (band)"
CONCEPT = "Nirvana"
BAND_TEXT = ("Nirvana was an American rock band formed in Aberdeen, Washington, in 1987. " * 40).strip()
CONCEPT_TEXT = "Nirvana is a concept in Indian religions representing the ultimate state. "
SUMMARY_TEXT = "Nirvana was an American rock band formed in Aberdeen, Washington."
BIO = "TheAudioDB's own paragraph about this artist."
# The parsed article, as `action=parse&prop=text` answers it: HTML with anchors.
# `BAND_PROSE` is what converting it must yield — the same text, link included.
BAND_HTML = f'<p>Nirvana was an <a href="/wiki/United_States">American</a> rock band. {BAND_TEXT}</p>'
BAND_PROSE = f"Nirvana was an [American](https://en.wikipedia.org/wiki/United_States) rock band. {BAND_TEXT}"
CONCEPT_HTML = f"<p>{CONCEPT_TEXT}</p>"

# --------------------------------------------------------------------------- #
# The two seams: discovery's JSON transport, MusicBrainz's cached GET.
# --------------------------------------------------------------------------- #
CALLS = []


def stub_json(routes):
    def fake(url, params=None, headers=None, timeout=None, ttl=None, host=None):
        params = dict(params or {})
        CALLS.append((url, params))
        return routes(url, params)
    discovery._json = fake


def stub_mb(payloads):
    def fake(endpoint, params=None, timeout=None, retries=None):
        return payloads.get(endpoint, {})
    intg.mb_get = fake
    intg.mb_get_cached = fake


DEF_ROUTES = {
    ("en.wikipedia.org", BAND): {"query": {"pages": [{"title": BAND, "extract": BAND_TEXT}]}},
    ("en.wikipedia.org", CONCEPT): {"query": {"pages": [{"title": CONCEPT, "extract": CONCEPT_TEXT}]}},
}
DEF_HTML = {BAND: BAND_HTML, CONCEPT: CONCEPT_HTML}


def routes(audiodb=None, relations=(("type", "wikidata", QID),), sitelink=BAND,
           search_qid=QID, html=True, extract=True):
    """Every endpoint the chain can reach, answering from the fixtures.

    `html`/`extract` off make the parsed-article / `prop=extracts` answers come
    back empty, which is how the two fallbacks are reached."""
    ad = audiodb or {}

    def one(url, params):
        if "theaudiodb.com" in url:
            if not ad.get("bio"):
                return {"artists": []}
            return {"artists": [{"strMusicBrainzID": MBID, "strArtist": "Nirvana",
                                 "strBiography": ad["bio"]}]}
        if "wikidata.org" in url:
            if params.get("action") == "wbsearchentities":
                return {"search": [{"id": search_qid}]} if search_qid else {"search": []}
            if params.get("props") == "sitelinks":
                item = {"sitelinks": {"enwiki": {"title": sitelink}}} if sitelink else {}
                return {"entities": {params.get("ids"): item}}
            return None
        if "rest_v1/page/summary/" in url:
            title = unquote(url.rsplit("/", 1)[-1]).replace("_", " ")
            return {"title": title, "extract": SUMMARY_TEXT,
                    "description": "American rock band",
                    "content_urls": {"desktop": {"page": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"}}}
        if "en.wikipedia.org" in url:
            if params.get("action") == "parse":
                # A missing page is an error object, exactly as MediaWiki sends it.
                if html and params.get("page") in DEF_HTML:
                    return {"parse": {"title": params["page"], "text": DEF_HTML[params["page"]]}}
                return {"error": {"code": "missingtitle"}}
            title = params.get("titles")
            if extract and title in (BAND, CONCEPT):
                return DEF_ROUTES[("en.wikipedia.org", title)]
            return None
        return None

    return one


def relations_payload(pairs):
    return {"artist/" + MBID: {"relations": [
        {"type": kind, "url": {"resource": f"https://www.wikidata.org/wiki/{qid}"}}
        for kind, _w, qid in pairs]}}


def wikipedia_titles():
    """Every article title asked for, by either request shape."""
    return [p.get("titles") or p.get("page") for u, p in CALLS
            if "api.php" in u and (p.get("titles") or p.get("page"))]


def clear():
    CALLS.clear()
    discovery._CACHE.clear()


# --------------------------------------------------------------------------- #
# 1) An MBID whose relations state a Wikidata item: that item's article.
# --------------------------------------------------------------------------- #
print("MBID -> Wikidata -> the sitelink's article")
clear()
stub_mb(relations_payload([("wikidata", "", QID)]))
stub_json(routes())
got = discovery.artist_description("Nirvana", mbid=MBID)
check("the whole article, by its sitelink title, links kept",
      got and got.get("title") == BAND and got.get("text") == BAND_PROSE
      and got.get("source") == "wikipedia" and got.get("chars") == len(BAND_PROSE), got)
check("the bare-name article was never asked",
      CONCEPT not in wikipedia_titles() and BAND in wikipedia_titles(),
      wikipedia_titles())
check("source_url is the article", got and got.get("source_url") ==
      "https://en.wikipedia.org/wiki/Nirvana_(band)", got)

# --------------------------------------------------------------------------- #
# 2) description_full off: the SAME resolved title, lead paragraph only.
# --------------------------------------------------------------------------- #
print("description_full off keeps the resolved title")
clear()
stub_mb(relations_payload([("wikidata", "", QID)]))
stub_json(routes())
short = discovery.artist_description("Nirvana", mbid=MBID, cfg={"description_full": False})
check("the summary is of the resolved title",
      short and short.get("title") == BAND and short.get("text") == SUMMARY_TEXT
      and short.get("short") == "American rock band", short)

# --------------------------------------------------------------------------- #
# 3) No Wikidata relation anywhere: the unchanged order still answers.
# --------------------------------------------------------------------------- #
print("no Wikidata link falls through, nothing breaks")
clear()
stub_mb(relations_payload([("discogs", "", "Q1")]))
stub_json(routes(audiodb={"bio": BIO}))
fell = discovery.artist_description("Nirvana", mbid=MBID)
check("TheAudioDB's own MBID record answers instead",
      fell and fell.get("source") == "audiodb" and fell.get("text") == BIO, fell)
check("no article was fetched for it", wikipedia_titles() == [], wikipedia_titles())

# --------------------------------------------------------------------------- #
# 4) A name alone: Wikidata's search resolves it before any title is guessed.
# --------------------------------------------------------------------------- #
print("name alone resolves through Wikidata search")
clear()
stub_mb({})
stub_json(routes())
named = discovery.artist_description("Nirvana")
check("the band's article, not the concept's",
      named and named.get("title") == BAND and named.get("text") == BAND_PROSE
      and CONCEPT_TEXT not in (named.get("text") or ""), named)

# --------------------------------------------------------------------------- #
# 5) The conversion itself: parsed article HTML -> prose with markdown links.
# --------------------------------------------------------------------------- #
print("article HTML -> prose with links")
conv = discovery._article_prose

check("a wikilink becomes markdown with an ABSOLUTE target",
      conv('<p>See <a href="/wiki/Kurt_Cobain">Kurt Cobain</a>.</p>')
      == "See [Kurt Cobain](https://en.wikipedia.org/wiki/Kurt_Cobain).",
      conv('<p>See <a href="/wiki/Kurt_Cobain">Kurt Cobain</a>.</p>'))

check("protocol-relative and already-absolute hrefs both resolve",
      conv('<p><a href="//en.wikipedia.org/wiki/Grunge">Grunge</a> from '
           '<a href="https://nirvana.com/">nirvana.com</a></p>')
      == "[Grunge](https://en.wikipedia.org/wiki/Grunge) from [nirvana.com](https://nirvana.com/)",
      conv('<p><a href="//en.wikipedia.org/wiki/Grunge">Grunge</a> from '
           '<a href="https://nirvana.com/">nirvana.com</a></p>'))

check("entities are decoded",
      conv("<p>Rock &amp; roll&nbsp;band</p>") == "Rock & roll band",
      conv("<p>Rock &amp; roll&nbsp;band</p>"))

check("a citation sup is dropped, its fragment link not invented",
      conv('<p>Formed in 1987.<sup class="reference">'
           '<a href="#cite_note-1">[1]</a></sup> In Aberdeen.</p>')
      == "Formed in 1987. In Aberdeen.",
      conv('<p>Formed in 1987.<sup class="reference">'
           '<a href="#cite_note-1">[1]</a></sup> In Aberdeen.</p>'))

check("a heading keeps its wiki shape, on its own line",
      conv('<h2 data-mw-anchor="History">History</h2><p>Text.</p>')
      == "== History ==\n\nText.",
      conv('<h2 data-mw-anchor="History">History</h2><p>Text.</p>'))

check("a deeper heading gets deeper markers",
      conv("<h3>Timeline</h3><p>Text.</p><h4>Deeper</h4><p>More.</p>")
      == "=== Timeline ===\n\nText.\n\n==== Deeper ====\n\nMore.",
      conv("<h3>Timeline</h3><p>Text.</p><h4>Deeper</h4><p>More.</p>"))

check("a stray h1 floors at the two markers the viewer accepts",
      conv("<h1>Nirvana</h1><p>Text.</p>") == "== Nirvana ==\n\nText.",
      conv("<h1>Nirvana</h1><p>Text.</p>"))

check("headings with no prose under them are gone",
      conv("<p>Prose.</p><h2>Band members</h2><h3>Timeline</h3>"
           "<h2>Discography</h2><ul><li>Bleach</li></ul>")
      == "Prose.\n\n== Discography ==\n\nBleach",
      conv("<p>Prose.</p><h2>Band members</h2><h3>Timeline</h3>"
           "<h2>Discography</h2><ul><li>Bleach</li></ul>"))

check("a parent heading whose body is its subsections survives",
      conv("<h2>History</h2><h3>1987</h3><p>Text.</p><h2>Legacy</h2>"
           "<h3>Empty</h3><h2>Discography</h2><ul><li>Bleach</li></ul>")
      == "== History ==\n\n=== 1987 ===\n\nText.\n\n== Discography ==\n\nBleach",
      conv("<h2>History</h2><h3>1987</h3><p>Text.</p><h2>Legacy</h2>"
           "<h3>Empty</h3><h2>Discography</h2><ul><li>Bleach</li></ul>"))

check("a navbox/table body is not prose either",
      conv('<p>Prose.</p><h2>See also</h2><table class="navbox"><tr>'
           '<td><a href="/wiki/Nirvana">Nirvana</a></td></tr></table>')
      == "Prose.",
      conv('<p>Prose.</p><h2>See also</h2><table class="navbox"><tr>'
           '<td><a href="/wiki/Nirvana">Nirvana</a></td></tr></table>'))

check("an empty parse answer converts to nothing, not to whitespace",
      conv("") == "" and conv('<p class="mw-empty-elt">\n</p>') == "")

# --------------------------------------------------------------------------- #
# 6) Either request answering nothing still describes the artist.
# --------------------------------------------------------------------------- #
print("parse -> explaintext -> summary, in that order")
clear()
stub_mb(relations_payload([("wikidata", "", QID)]))
stub_json(routes(html=False))
plain = discovery.artist_description("Nirvana", mbid=MBID)
check("no parsed HTML: the explaintext extract answers, link-free",
      plain and plain.get("text") == BAND_TEXT and "](" not in plain.get("text", ""),
      plain)

clear()
stub_json(routes(html=False, extract=False))
fallback = discovery.artist_description("Nirvana", mbid=MBID)
check("neither answer: the REST summary answers",
      fallback and fallback.get("text") == SUMMARY_TEXT
      and fallback.get("source") == "wikipedia", fallback)

print(f"\n{'FAILURES: ' + ', '.join(FAILS) if FAILS else 'all checks passed'}")
raise SystemExit(1 if FAILS else 0)
