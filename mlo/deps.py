"""Optional third-party dependencies.

Centralises feature detection so the rest of the package can rely on
HAS_MUTAGEN / HAS_PIL and the mutagen classes (None when not installed).
"""

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


try:
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.mp3 import MP3
    from mutagen.id3 import (TXXX, USLT, COMM, UFID, Encoding, TextFrame,
                             Frames, APIC)
    from mutagen.mp4 import MP4, MP4FreeForm, MP4Cover
    from mutagen.flac import Picture
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    FLAC = OggVorbis = OggOpus = MP3 = MP4 = MP4FreeForm = None
    TXXX = USLT = COMM = UFID = Encoding = TextFrame = Frames = APIC = None
    MP4Cover = Picture = None


try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

