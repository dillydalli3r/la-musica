# la musica 3.14.1 — an alias is a translation, not decoration

Fixes one thing, reported on the artist pages: **Radiohead was coming up as
"Radiohead (レディオヘッド)"**.

MusicBrainz marks レディオヘッド as Radiohead's *primary* alias, and the ladder
took a primary alias whatever its locale — even when the name beside it was
already written in the script the reader reads. That is noise: nothing about an
English reader's "Radiohead" was clarified by its katakana, and the same rule
was romanizing Japanese names for readers who had asked for Japanese.

**The rule now is the reader's, not the name's.** An alias must be at least as
*readable* as the name it annotates: when the name is already written in the
script your `locale` reads and the alias is not, the alias is dropped. An alias
in your own script always passes — which is what keeps the two cases the feature
exists for:

| name | your locale | shown |
| --- | --- | --- |
| `Radiohead` | `en` | *(nothing — with `en` it is the name itself)* |
| `Radiohead` | `ja` | `Radiohead (レディオヘッド)` |
| `宇多田ヒカル` | `en` | `宇多田ヒカル (Hikaru Utada)` |
| `宇多田ヒカル` | `ja` | *(nothing — the name is already readable)* |
| `Кино` | `en` | `Кино (Kino)` |
| `Кино` | `ru` | *(nothing)* |
| `Lost Umbrella` (name `ロストアンブレラ`, no locale set) | — | `ロストアンブレラ (Lost Umbrella)` |

The script is decided by a table covering the languages the app ships (Japanese,
Chinese, Korean, Russian/Ukrainian/Bulgarian/Serbian, Greek, Hebrew, Arabic,
Thai, Hindi/Marathi/Nepali); everything else — English included — reads Latin,
which is also what a romanization is written in. No language detector, no extra
MusicBrainz request.

**Verified on the live data you reported**: `GET /api/mb/artist/a74b1b7f-…`
(Radiohead) now carries an empty alias — and so do all 300 of its release groups
— while `宇多田ヒカル` still shows `Hikaru Utada`, and her kana titles still get
their readings (`桜流し (Sakura Nagashi)`, `初恋 (Hatsukoi)`). The alias suite
pins every row of the table above, both mirrors included.
