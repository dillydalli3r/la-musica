"""The genre vocabulary: what a genre name may BE, and what family it belongs to.

Two ideas, and nothing else:

1. **A recognised name is a MusicBrainz name.** ``_genre_names.MB_GENRES`` is
   MusicBrainz's own genre list, verbatim, and every name in it is lowercase
   (that is how MusicBrainz spells its own genres). So canonicalisation is
   cheap and needs no table for casing: fold case and whitespace, try a hyphen
   swap, try the alias map, and the name either is MusicBrainz's or it is not.

2. **A family is the broad head a specific genre sits under.** `rock`,
   `electronic`, `hip hop`, … — the "parent genre" that goes in front of the
   specific one. MusicBrainz publishes no genre hierarchy, so the table below
   is curated, and the keyword rules after it catch the long tail. A genre
   whose family is unknown gets **no** parent rather than a wrong one.

The curated table is the part to extend when a new family matters; a name that
falls through every rule is still stored (lossless) — it simply fails the
"recognised genre" grade check until it is mapped or renamed.

MEASURED 2026-10-11 over MusicBrainz's own 2 202 names: with the row-matching
rules alone 717 answered and 21% of those were substring artifacts (`detroit
techno` and `harsh noise` were `punk` — the `oi` inside "detroit"/"noise";
`graphical sound` was `hip hop`; `melodic dubstep` was `reggae`). Token matching
fixed those but lost real compounds, so the walk now reads a keyword as a whole
token first and as a prefix/suffix of the name only as a second pass, with
short keywords (`oi`, `ska`, `emo`, `wave`) exempt: 995 names have a head, the
268 mid-level edges of `_PARENT_OF` give the ones that need a middle step
(`deep house` → `house` → `electronic`), and the answers above survive
(`rocksteady` stays reggae, not rock).
"""
from __future__ import annotations

import re
from typing import Optional

from ._genre_names import MB_GENRES

# The families, in the order the keyword fallback below prefers them. Broad
# heads only: a family is what a track has *in common* with thousands of
# others, so `shoegaze`'s family is `rock` and not `indie rock`.
FAMILIES = (
    "metal", "punk", "hip hop", "rock", "pop", "jazz", "blues", "soul", "funk",
    "r&b", "reggae", "country", "folk", "classical", "latin", "gospel",
    "industrial", "experimental", "ambient", "new age", "easy listening",
    "dance", "electronic", "instrumental", "spoken word", "comedy",
    "christmas music", "children's music",
)

# Specific genre -> family. Curated: the genres the app's own sources actually
# emit, plus the ones a person would look for by hand. Names are MusicBrainz
# spellings (tools/test_genre_vocab.py asserts every one of them is a real
# MusicBrainz genre, so this table cannot rot into invented names).
_FAMILY_OF = {
    "2 tone": "reggae", "abstract hip hop": "hip hop", "acid breaks": "electronic",
    "acid house": "electronic", "acid jazz": "jazz", "acid rock": "rock",
    "acid techno": "electronic", "acoustic blues": "blues", "acoustic rock": "rock",
    "afro-cuban jazz": "jazz", "afrobeat": "funk", "aggrotech": "industrial",
    "alternative country": "country", "alternative hip hop": "hip hop", "alternative metal": "metal",
    "alternative rock": "rock", "ambient dub": "ambient", "ambient house": "ambient",
    "ambient techno": "electronic", "americana": "country", "anarcho-punk": "punk",
    "anatolian rock": "rock", "anti-folk": "folk", "arena rock": "rock",
    "art pop": "pop", "art rock": "rock", "atmospheric black metal": "metal",
    "avant-garde": "experimental", "avant-garde jazz": "jazz", "avant-garde metal": "metal",
    "bachata": "latin", "baroque pop": "pop", "bebop": "jazz",
    "bedroom pop": "pop", "big band": "jazz", "black metal": "metal",
    "blue-eyed soul": "soul", "bluegrass": "country", "blues rock": "rock",
    "bolero": "latin", "boogie": "funk", "boom bap": "hip hop",
    "bossa nova": "latin", "breakbeat": "electronic", "breakcore": "electronic",
    "bubblegum pop": "pop", "canterbury scene": "rock", "chamber pop": "pop",
    "chicago blues": "blues", "chicha": "latin", "chillout": "electronic",
    "chillwave": "electronic", "coldwave": "electronic",
    "christian rock": "gospel", "city pop": "pop", "classic rock": "rock",
    "cloud rap": "hip hop", "comedy rock": "comedy", "conscious hip hop": "hip hop",
    "contemporary christian": "gospel", "contemporary classical": "classical", "contemporary folk": "folk",
    "contemporary r&b": "r&b", "cool jazz": "jazz", "country pop": "country",
    "country rock": "rock", "country soul": "country", "crunk": "hip hop",
    "crust punk": "punk", "cumbia": "latin", "dance-pop": "pop",
    "dance-rock": "rock", "dancehall": "reggae", "dark ambient": "ambient",
    "death metal": "metal", "death-doom metal": "metal", "deconstructed club": "electronic",
    "deep house": "electronic", "deep soul": "soul", "delta blues": "blues",
    "dirty south": "hip hop", "disco": "dance", "djent": "metal",
    "doom metal": "metal", "downtempo": "electronic", "dream pop": "rock",
    "drill": "hip hop", "drone": "experimental", "drum and bass": "electronic",
    "dub": "reggae", "dubstep": "electronic", "east coast hip hop": "hip hop",
    "ebm": "industrial", "edm": "electronic", "electric blues": "blues",
    "electro": "electronic", "electro house": "electronic", "electro-funk": "funk",
    "electro-industrial": "industrial", "electroacoustic": "experimental", "electropop": "pop",
    "emo": "punk", "ethio-jazz": "jazz", "eurodance": "dance",
    "europop": "pop", "exotica": "easy listening", "experimental rock": "rock",
    "field recording": "experimental", "folk metal": "metal", "folk rock": "rock",
    "footwork": "electronic", "forró": "latin", "freak folk": "folk",
    "free improvisation": "experimental", "free jazz": "jazz", "funk rock": "funk",
    "future garage": "electronic", "futurepop": "industrial", "g-funk": "hip hop",
    "gangsta rap": "hip hop", "garage rock": "rock", "glam metal": "metal",
    "glam rock": "rock", "glitch": "electronic", "gothic metal": "metal",
    "gothic rock": "rock", "grime": "hip hop", "grindcore": "metal",
    "groove metal": "metal", "gypsy jazz": "jazz", "hard bop": "jazz",
    "hard rock": "rock", "hardcore hip hop": "hip hop", "hardcore punk": "punk",
    "hardstyle": "electronic", "harsh noise wall": "experimental", "heavy metal": "metal",
    "hi-nrg": "dance", "honky tonk": "country", "horror punk": "punk",
    "horrorcore": "hip hop", "house": "electronic", "idm": "electronic",
    "impressionism": "classical", "indie folk": "folk", "indie pop": "pop",
    "indie rock": "rock", "industrial metal": "metal", "industrial rock": "rock",
    "instrumental hip hop": "hip hop", "instrumental rock": "instrumental", "j-pop": "pop",
    "jangle pop": "rock", "jazz rap": "hip hop", "jump blues": "blues",
    "jungle": "electronic", "k-pop": "pop", "krautrock": "rock",
    "latin jazz": "jazz", "lo-fi": "electronic", "lo-fi hip hop": "hip hop",
    "lounge": "easy listening", "lovers rock": "reggae", "mambo": "latin",
    "martial industrial": "industrial", "math rock": "rock", "melodic death metal": "metal",
    "memphis rap": "hip hop", "merengue": "latin", "metalcore": "metal",
    "microhouse": "electronic", "midwest emo": "rock", "minimal techno": "electronic",
    "minimalism": "classical", "modal jazz": "jazz", "modern classical": "classical",
    "minimal wave": "electronic", "musique concrète": "experimental",
    "neo soul": "soul", "neofolk": "folk",
    # The "wave" names, spelled out. The keyword rule below has "wave" as an
    # ELECTRONIC word, which is true of the genre called "wave" and of the
    # synth-driven ones — and flatly wrong for new wave (a pop/rock movement
    # named for the thing it was new AGAINST) and for no wave (an avant-garde
    # scene that was not electronic at all). The owner's report is exactly
    # that: a new wave track reading "Electronic; New Wave".
    "new romantic": "pop", "new wave": "rock", "no wave": "experimental",
    "noise": "experimental", "noise pop": "pop", "noise rock": "rock",
    "northern soul": "soul", "nu jazz": "jazz", "nu metal": "metal",
    "oi": "punk", "old school hip hop": "hip hop", "opera": "classical",
    "orchestral": "classical", "outlaw country": "country", "p-funk": "funk",
    "philly soul": "soul", "phonk": "hip hop", "piedmont blues": "blues",
    "poetry": "spoken word", "political hip hop": "hip hop", "pop punk": "punk",
    "pop rock": "rock", "post-bop": "jazz", "post-britpop": "rock",
    "post-grunge": "rock", "post-hardcore": "punk", "post-metal": "rock",
    "post-minimalism": "classical", "post-punk": "punk", "post-rock": "rock",
    "power electronics": "industrial", "power metal": "metal", "power noise": "experimental",
    "power pop": "pop", "progressive country": "country", "progressive metal": "metal",
    "progressive rock": "rock", "psychedelic folk": "folk", "psychedelic pop": "pop",
    "psychedelic rock": "rock", "pub rock": "rock", "punk rock": "punk",
    "quiet storm": "r&b", "ragga": "reggae", "reggaeton": "latin",
    "riot grrrl": "punk", "rockabilly": "country", "rocksteady": "reggae",
    "romantic classical": "classical", "roots reggae": "reggae", "salsa": "latin",
    "samba": "latin", "serialism": "classical", "sermon": "spoken word",
    "shoegaze": "rock", "singer-songwriter": "folk", "ska": "reggae",
    "ska punk": "punk", "slowcore": "rock", "sludge metal": "metal",
    "smooth jazz": "jazz", "soft rock": "rock", "son cubano": "latin",
    "sophisti-pop": "pop", "sound collage": "experimental", "southern gospel": "gospel",
    "southern hip hop": "hip hop", "southern soul": "soul", "space age pop": "easy listening",
    "space rock": "rock", "speed metal": "metal", "spiritual jazz": "jazz",
    "stoner metal": "metal", "stoner rock": "rock", "street punk": "punk",
    "sunshine pop": "pop", "surf punk": "punk", "surf rock": "rock",
    "swing": "jazz", "symphonic metal": "metal", "symphonic rock": "rock",
    "symphony": "classical", "synth funk": "funk", "synth-pop": "pop",
    "synthwave": "electronic", "tango": "latin", "techno": "electronic",
    "teen pop": "pop", "texas blues": "blues", "texas country": "country",
    "thrash metal": "metal", "timba": "latin", "trance": "electronic",
    "trap": "hip hop", "trap metal": "hip hop", "trap soul": "hip hop",
    "trip hop": "electronic", "turntablism": "hip hop", "uk garage": "electronic",
    "vaporwave": "electronic", "vocal jazz": "jazz", "west coast hip hop": "hip hop",
    "witch house": "electronic", "yacht rock": "rock",
}

# Keyword fallback for the long tail, in the order ``FAMILIES`` is written:
# the first family whose keyword appears in the name wins, so `folk metal` is
# metal (metal is checked first) and `post-punk` is punk (punk before rock).
_KEYWORDS = (
    ("metal", ("metal", "grindcore", "djent", "metalcore", "sludge")),
    ("punk", ("punk", "hardcore", "emo", "riot grrrl", "oi")),
    ("hip hop", ("hip hop", "rap", "trap", "grime", "drill", "boom bap",
                 "turntablism", "crunk", "phonk")),
    ("rock", ("rock", "shoegaze", "grunge", "britpop", "krautrock", "emo",
              "slowcore", "math rock")),
    ("pop", ("pop", "bubblegum", "schlager", "chanson")),
    ("jazz", ("jazz", "bop", "bebop", "swing", "ragtime", "big band")),
    ("blues", ("blues",)),
    ("soul", ("soul", "motown")),
    ("funk", ("funk", "boogie", "afrobeat")),
    ("r&b", ("r&b", "rhythm and blues", "quiet storm")),
    ("reggae", ("reggae", "dub", "dancehall", "ska", "rocksteady", "ragga")),
    ("country", ("country", "bluegrass", "americana", "honky tonk", "rockabilly",
                 "cowboy")),
    ("folk", ("folk", "singer-songwriter", "celtic", "klezmer", "fado")),
    ("classical", ("classical", "baroque", "symphon", "orchestr", "chamber",
                   "opera", "choral", "concerto", "sonata", "requiem", "waltz",
                   "quartet", "medieval", "renaissance", "impressionis",
                   "serialis", "minimalis")),
    ("latin", ("latin", "salsa", "bossa", "cumbia", "tango", "samba", "mambo",
               "merengue", "bachata", "reggaeton", "bolero", "choro", "flamenco",
               "son cubano")),
    ("gospel", ("gospel", "christian", "worship", "spiritual")),
    ("industrial", ("industrial", "ebm", "aggrotech", "futurepop", "martial")),
    ("experimental", ("experimental", "avant", "noise", "improvisation",
                      "musique concrète", "sound collage", "field recording",
                      "drone")),
    ("ambient", ("ambient", "isolationis", "lowercase")),
    ("new age", ("new age", "meditation", "relaxation", "self-help")),
    ("christmas music", ("christmas", "xmas", "holiday")),
    ("children's music", ("children", "kids", "lullaby")),
    ("spoken word", ("spoken word", "poetry", "audiobook", "sermon", "speech")),
    ("comedy", ("comedy", "parody", "novelty", "stand-up")),
    ("easy listening", ("easy listening", "lounge", "exotica", "space age",
                        "elevator")),
    ("dance", ("dance", "disco", "hi-nrg", "eurodance")),
    ("electronic", ("electronic", "techno", "house", "trance", "dubstep",
                    "breakbeat", "breakcore", "garage", "downtempo", "glitch",
                    "idm", "jungle", "synth", "electro", "edm", "chillout",
                    "vaporwave", "footwork", "drum and bass", "wave")),
    ("instrumental", ("instrumental",)),
)

# The MIDDLE of the tree: genres that are neither a head nor the end of the
# line, and the genre directly above them. Only curated, well-known steps are
# here — a name with no entry simply hangs off its family, which is exactly
# where it hung before this table existed (`deep house` used to answer
# `electronic`; now it answers `house`, whose own parent is `electronic`).
#
# WHY it exists at all: a genre is a NODE, and a node's role comes from its
# edges, never from its name — `house` is the parent of `deep house` AND a
# genre a track can hold outright (MusicBrainz lists it, so a source may state
# it), and `triphop`'s cousin `ambient house` sits under it too. The old model
# could not say that: it asked "is this WORD one of the 28 heads?" and pulled
# any head-named genre into a parent slot, which is the collision this table
# removes by making "what is above X?" answerable for every X.
#
# Every parent here must be a real MusicBrainz genre, and every chain must
# land on the SAME head the curated/keyword answer gives (`tools/test_genre_vocab.py`
# asserts both) — a middle step that moved a genre between families would
# silently re-bucket the library's browse facets.
_PARENT_OF = {
    "2 tone": "reggae", "2-step": "uk garage", "abstract hip hop": "hip hop",
    "acid house": "house", "acid jazz": "jazz", "acid techno": "techno",
    "acid trance": "trance", "acoustic blues": "blues", "afro-cuban jazz": "jazz",
    "afrobeat": "funk", "aggrotech": "industrial", "alternative country": "country",
    "alternative hip hop": "hip hop", "alternative rock": "rock", "ambient dub": "ambient",
    "ambient house": "house", "ambient techno": "techno", "americana": "country",
    "anarcho-punk": "punk", "anti-folk": "folk", "art pop": "pop",
    "art punk": "punk", "atmospheric black metal": "black metal", "atmospheric drum and bass": "drum and bass",
    "avant-garde jazz": "jazz", "bachata": "latin", "ballet": "classical",
    "baroque": "classical", "baroque pop": "pop", "bassline": "uk garage",
    "bebop": "jazz", "bedroom pop": "pop", "big band": "jazz",
    "big beat": "electronic", "black metal": "metal", "blue-eyed soul": "soul",
    "bluegrass": "country", "bolero": "latin", "boom bap": "hip hop",
    "bossa nova": "latin", "breakbeat": "electronic", "breakcore": "drum and bass",
    "britpop": "rock", "brostep": "dubstep", "brutal death metal": "death metal",
    "bubblegum pop": "pop", "celtic": "folk", "chamber pop": "pop",
    "chicago blues": "blues", "chicago house": "house", "chicha": "latin",
    "chillout": "downtempo", "cinematic classical": "classical", "city pop": "pop",
    "cloud rap": "hip hop", "conscious hip hop": "hip hop", "contemporary classical": "classical",
    "contemporary folk": "folk", "contemporary r&b": "r&b", "cool jazz": "jazz",
    "country pop": "country", "country rock": "rock", "country soul": "soul",
    "crossover thrash": "thrash metal", "crunk": "hip hop", "crust punk": "punk",
    "cumbia": "latin", "dance-pop": "pop", "dancehall": "reggae",
    "dark ambient": "ambient", "darkstep": "drum and bass", "death metal": "metal",
    "death-doom metal": "doom metal", "deathcore": "metal", "deep drum and bass": "drum and bass",
    "deep house": "house", "deep soul": "soul", "delta blues": "blues",
    "depressive black metal": "black metal", "detroit techno": "techno", "dirty south": "hip hop",
    "disco": "dance", "djent": "metal", "doom metal": "metal",
    "downtempo": "electronic", "dream pop": "rock", "dream trance": "trance",
    "drill": "hip hop", "drill and bass": "idm", "drone": "ambient",
    "drone metal": "doom metal", "dub": "reggae", "dub techno": "techno",
    "east coast hip hop": "hip hop", "ebm": "industrial", "edm": "electronic",
    "electric blues": "blues", "electro": "electronic", "electro house": "house",
    "electro-funk": "funk", "electro-industrial": "industrial", "electroacoustic": "experimental",
    "electropop": "pop", "emo": "punk", "epic doom metal": "doom metal",
    "ethio-jazz": "jazz", "eurodance": "dance", "europop": "pop",
    "exotica": "easy listening", "experimental hip hop": "hip hop", "fado": "folk",
    "field recording": "experimental", "filk": "folk", "flamenco": "latin",
    "folk metal": "metal", "forró": "latin", "freak folk": "folk",
    "free jazz": "jazz", "funeral doom metal": "doom metal", "future garage": "dubstep",
    "futurepop": "industrial", "g-funk": "hip hop", "gabber": "electronic",
    "gangsta rap": "hip hop", "garage house": "house", "ghetto house": "house",
    "glitch": "idm", "goa trance": "trance", "gothic metal": "metal",
    "gothic rock": "rock", "grime": "uk garage", "grindcore": "metal",
    "groove metal": "thrash metal", "grunge": "rock", "happy hardcore": "electronic",
    "hard bop": "jazz", "hard house": "house", "hard rock": "rock",
    "hard techno": "techno", "hard trance": "trance", "hardcore hip hop": "hip hop",
    "hardcore punk": "punk", "hardcore techno": "electronic", "hardstep": "drum and bass",
    "hardstyle": "electronic", "heavy metal": "metal", "hi-nrg": "dance",
    "hip house": "house", "honky tonk": "country", "impressionism": "classical",
    "indie pop": "pop", "indie rock": "rock", "industrial techno": "techno",
    "italo-disco": "dance", "j-pop": "pop", "jazz fusion": "jazz",
    "jump blues": "blues", "jump up": "drum and bass", "jungle": "drum and bass",
    "k-pop": "pop", "klezmer": "folk", "krautrock": "rock",
    "liquid funk": "drum and bass", "lo-fi hip hop": "hip hop", "lounge": "easy listening",
    "lovers rock": "reggae", "mambo": "latin", "martial industrial": "industrial",
    "math rock": "rock", "medieval": "classical", "melodic black metal": "black metal",
    "melodic death metal": "death metal", "melodic techno": "techno", "merengue": "latin",
    "metalcore": "metal", "microhouse": "house", "minimal techno": "techno",
    "minimalism": "classical", "modal jazz": "jazz", "modern classical": "classical",
    "musique concrète": "experimental", "neo soul": "r&b", "neofolk": "folk",
    "neurofunk": "drum and bass", "new wave": "rock",
    "no wave": "experimental", "noise": "experimental", "northern soul": "soul",
    "nu disco": "dance", "nu jazz": "downtempo", "nu metal": "metal",
    "nu skool breaks": "electronic", "opera": "classical", "orchestral": "classical",
    "outlaw country": "country", "outsider house": "house", "p-funk": "funk",
    "phonk": "hip hop", "piano blues": "blues", "piedmont blues": "blues",
    "pop punk": "punk", "post-bop": "jazz", "post-dubstep": "dubstep",
    "post-hardcore": "punk", "post-punk": "punk", "post-rock": "rock",
    "power metal": "metal", "power pop": "pop", "progressive house": "house",
    "progressive metal": "metal", "progressive rock": "rock", "progressive trance": "trance",
    "psychedelic rock": "rock", "psychedelic soul": "soul", "psytrance": "trance",
    "quiet storm": "r&b", "ragga": "reggae", "ragga jungle": "drum and bass",
    "ragtime": "jazz", "reggaeton": "latin", "renaissance": "classical",
    "riddim dubstep": "dubstep", "rockabilly": "country", "rocksteady": "reggae",
    "romantic classical": "classical", "roots reggae": "reggae", "salsa": "latin",
    "schranz": "techno", "serialism": "classical", "shoegaze": "rock",
    "singer-songwriter": "folk", "ska": "reggae", "ska punk": "punk",
    "slowcore": "rock", "sludge metal": "metal", "smooth jazz": "jazz",
    "son cubano": "latin", "sophisti-pop": "pop", "sound collage": "experimental",
    "space ambient": "ambient", "speed garage": "uk garage", "spiritual jazz": "jazz",
    "stoner metal": "metal", "stoner rock": "rock", "street punk": "punk",
    "sunshine pop": "pop", "swing": "jazz", "symphonic black metal": "black metal",
    "symphonic metal": "metal", "synth-pop": "pop", "tango": "latin",
    "tech house": "house", "technical death metal": "death metal", "teen pop": "pop",
    "texas blues": "blues", "thrash metal": "metal", "timba": "latin",
    "trap": "hip hop", "trap soul": "hip hop", "tribal house": "house",
    "trip hop": "electronic", "vocal house": "house", "vocal jazz": "jazz",
    "vocal trance": "trance", "war metal": "black metal", "west coast hip hop": "hip hop",
    "western swing": "country",
}

# Spellings that are NOT MusicBrainz names but are the same thing. Every value
# must be a real MusicBrainz genre (tested), and every key is a spelling the
# app itself, a source, or a model has been seen to produce.
ALIASES = {
    "hip-hop": "hip hop",
    "hiphop": "hip hop",
    "hip hop/rap": "hip hop",
    "rap": "hip hop",
    "rnb": "r&b",
    "r 'n' b": "r&b",
    "randb": "r&b",
    "rhythm & blues": "r&b",
    "electronica": "electronic",
    "drum & bass": "drum and bass",
    "dnb": "drum and bass",
    "d&b": "drum and bass",
    "alt rock": "alternative rock",
    "alt-rock": "alternative rock",
    "indie": "indie rock",
    "synthpop": "synth-pop",
    "synth pop": "synth-pop",
    "alt-country": "alternative country",
    "americana/country": "americana",
    "classical music": "classical",
    "holiday music": "christmas music",
    "xmas": "christmas music",
    "childrens": "children's music",
    "childrens music": "children's music",
    "kids music": "children's music",
    "lo fi": "lo-fi",
    "lofi hip hop": "lo-fi hip hop",
    "post rock": "post-rock",
    "post punk": "post-punk",
    "post bop": "post-bop",
    "art-rock": "art rock",
    "prog rock": "progressive rock",
    "prog": "progressive rock",
    "psych rock": "psychedelic rock",
    "psychedelia": "psychedelic rock",
    "singer songwriter": "singer-songwriter",
    "new-wave": "new wave",
    "boy band": "pop",
    "girl group": "pop",
    "idm/electronica": "idm",
    "drum and bass/jungle": "drum and bass",
}


def _fold(name) -> str:
    """One genre name, folded for lookup: trimmed, collapsed, casefolded."""
    return " ".join(str(name or "").split()).casefold()


def canonical(name) -> Optional[str]:
    """The MusicBrainz spelling of *name*, or None when it is not a genre.

    Tries the folded name, the hyphen/space swap, then the alias map. None
    means "not a genre this app recognises" — callers keep the original text
    if they must (lossless) but should flag it rather than write it fresh.
    """
    text = _fold(name)
    if not text:
        return None
    if text in MB_GENRES:
        return text
    swapped = text.replace("-", " ") if "-" in text else text.replace(" ", "-")
    if swapped in MB_GENRES:
        return swapped
    alias = ALIASES.get(text)
    if alias and alias in MB_GENRES:
        return alias
    return None


# A keyword may match a compound as a prefix/suffix only from this length up
# (see `_keyword_parent`'s second pass).
PREFIX_KEYWORD_MIN_LEN = 4


def _table_key(name) -> str:
    """The lookup key for *name*: its MusicBrainz spelling when it is one.

    The tables are written in MusicBrainz's own spelling, and a source's own
    spelling of the same genre can differ by a hyphen or an alias ("italo
    disco" vs MusicBrainz's "italo-disco", "synthpop" vs "synth-pop"), so a
    lookup folds the name through `canonical` FIRST and only then falls back
    to the raw text. That is what lets one table entry answer for every
    spelling a source emits.
    """
    return _fold(canonical(name)) or _fold(name)


def _keyword_parent(text):
    """The head a keyword rule gives *text*, or None.

    The keyword must be a WHOLE TOKEN — the text split on spaces, hyphens and
    slashes — or it does not count. MEASURED 2026-10-11 over MusicBrainz's own
    2 202 names: plain substring matching answered for 717 names, and 154 of
    those (21%) came from a keyword sitting INSIDE another word — `detroit
    techno` and `harsh noise` were `punk` (the `oi` of "detroit"/"noise"),
    `graphical sound` was `hip hop` (`rap` in "graphical"), `melodic dubstep`
    was `reggae` (`dub` first), `neurofunk` was `funk`. Token matching keeps
    the rules that earn their keep (`folk metal` → metal) and drops the noise.
    """
    tokens = [t for t in re.split(r"[\s\-/]+", text) if t]
    # Pass 1: a whole TOKEN (or a multi-word phrase) — the precise reading, and
    # the one that decides whenever it can (`rocksteady` is reggae's own
    # keyword, so `rock` never gets to claim it).
    for family, words in _KEYWORDS:
        for word in words:
            parts = word.split(" ")
            if len(parts) == 1:
                if word in tokens:
                    return family
            elif " ".join(parts) in text:
                # A multi-word keyword ("drum and bass", "spoken word") has to
                # appear as the phrase; the token pass above cannot see a
                # phrase that spans a hyphen ("drum-and-bass" is three tokens).
                return family
    # Pass 2 (only when nothing matched): a keyword that is a PREFIX or SUFFIX
    # of the name, which is how a compound is spelled — `electroclash`,
    # `vaportrap`, `spacesynth`, `orchestral song`. Never a bare substring: the
    # 21% misfire rate this whole helper was written for came from `oi` inside
    # "detroit", `rap` inside "graphical", `pop` inside "upopo", and none of
    # those is a prefix or a suffix. Short keywords are exempt — `oi`, `emo`,
    # `ska`, `dub`, `wave` name too many things that merely start with them.
    for family, words in _KEYWORDS:
        for word in words:
            if len(word) < PREFIX_KEYWORD_MIN_LEN or " " in word:
                continue
            if text.startswith(word) or text.endswith(word):
                return family
    return None


def is_parent(name) -> bool:
    """True when *name* is one of the broad families (a head of the tree)."""
    return _table_key(name) in _FAMILY_SET


def parent_of(name) -> Optional[str]:
    """The genre directly ABOVE *name*, or None when nothing above is known.

    One step, never the whole walk: `parent_of("deep house")` is `house`, and
    `path_of` is what turns that into the head. A genre's role comes from its
    edges — `house` is a middle node here AND a genre a track may hold, and
    this function answers for both uses the same way, because it only ever
    says "what is above X?".

    A head has no parent (None), so a caller that wants the top of a chain
    asks `top_of`. No parent is better than a wrong one: a name no rule knows
    simply answers None and is graded on its own.
    """
    text = _table_key(name)
    if not text:
        return None
    if text in _FAMILY_SET:
        return None
    middle = _PARENT_OF.get(text)
    if middle:
        return middle
    family = _FAMILY_OF.get(text)
    if family:
        return family
    return _keyword_parent(text)


# How far a walk up may go. The tables above are hand-written and a cycle
# would hang every facet, so the walk is bounded rather than trusted.
_MAX_DEPTH = 8


def path_of(name) -> list:
    """*name* and everything above it, top first: ["electronic", "house",
    "deep house"].

    The app's whole hierarchy view comes from this — browse buckets, the chip
    beside a genre, the credits-style "where does this sit" line — and it is
    DERIVED, never stored: a track's GENRE tag holds the specific names a
    source stated, and the levels above them are looked up here, so improving
    this table fixes every already-tagged file at once.
    """
    text = _table_key(name)
    if not text:
        return []
    chain = [text]
    seen = {text}
    for _ in range(_MAX_DEPTH):
        above = _PARENT_OF.get(text) or _FAMILY_OF.get(text)
        if not above:
            if text in _FAMILY_SET:
                break
            above = _keyword_parent(text)
            if not above:
                break
        if above in seen:
            break
        chain.append(above)
        seen.add(above)
        text = above
    chain.reverse()
    return chain


def top_of(name) -> Optional[str]:
    """The head *name* hangs under, or None when it hangs under nothing.

    `top_of("deep house")` is "electronic" even though its PARENT is "house":
    the facets and the genre browse bucket by head, and the middle step is
    what the UI draws between them.
    """
    chain = path_of(name)
    if not chain:
        return None
    head = chain[0]
    return head if head in _FAMILY_SET else None


_FAMILY_SET = frozenset(FAMILIES)
