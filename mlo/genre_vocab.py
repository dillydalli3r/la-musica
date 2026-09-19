"""The genre vocabulary: what a genre name may BE, and what family it belongs to.

Two ideas, and nothing else:

1. **A recognised name is a MusicBrainz name.** ``_genre_names.MB_GENRES`` is
   MusicBrainz's own genre list, verbatim, and every name in it is lowercase
   (that is how MusicBrainz spells its own genres). So canonicalisation is
   cheap and needs no table for casing: fold case and whitespace, try a hyphen
   swap, try the alias map, and the name either is MusicBrainz's or it is not.

2. **A family is the broad head a specific genre sits under.** `rock`,
   `electronic`, `hip hop`, … — the "parent genre" the app appends after the
   specific one. MusicBrainz publishes no genre hierarchy, so the table below
   is curated, and the keyword rules after it catch the long tail. A genre
   whose family is unknown gets **no** parent rather than a wrong one.

The curated table is the part to extend when a new family matters; a name that
falls through every rule is still stored (lossless) — it simply fails the
"recognised genre" grade check until it is mapped or renamed.
"""
from __future__ import annotations

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
    "musique concrète": "experimental", "neo soul": "soul", "neofolk": "folk",
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

# Spellings that are NOT MusicBrainz names but are the same thing. Every value
# must be a real MusicBrainz genre (tested), and every key is a spelling the
# app itself, RYM, or a model has been seen to produce.
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


def is_parent(name) -> bool:
    """True when *name* is one of the broad families."""
    return _fold(name) in _FAMILY_SET


def parent_of(name) -> Optional[str]:
    """The broad family *name* belongs to, or None when none is known.

    No family is better than a wrong one: an unmapped genre simply gets no
    parent slot, and the track is graded on the genres it does have.
    """
    text = _fold(name) or _fold(canonical(name))
    if not text:
        return None
    if text in _FAMILY_SET:
        return text
    family = _FAMILY_OF.get(text)
    if family:
        return family
    for family, words in _KEYWORDS:
        if any(w in text for w in words):
            return family
    return None


_FAMILY_SET = frozenset(FAMILIES)
