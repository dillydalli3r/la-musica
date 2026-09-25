# la musica 3.20.3 - the layout pass removes what is excess, and does it on every import

Issue #46: the Optimization panel found stray files and left them there. "Any
extra / unrecognized files should be removed and done so automatically by the
app." They are now — through the Trash, which can put every one of them back.

## The scan reports; the optimize pass settles

Script 20 is renamed to match what it actually does, and it acts on six more
findings than it used to. With `layout_apply` (ON, the default) it now removes
to the app's Trash:

- a **stray file** inside an album — not audio, artwork or a known sidecar (an
  nfo, a db, a stray text file);
- a **folder inside an album** that is neither a disc folder (`CD1`, `Disc 2`,
  …) nor holds any audio;
- an **album folder with no audio in it** — the `cover.jpg`-only shell an
  interrupted import leaves;
- a **foreign folder in the music folder root** that holds no audio;
- an **album-less artist folder** (already removed before, unchanged);
- the **`.mlo_*` leftovers** of the old layout.

The three proven fixes are unchanged: a wrong-case name is renamed to
`naming_script`'s spelling, and audio outside any album folder is moved into the
album its own tags name.

**Nothing is deleted.** Every removal is the same move the "Remove from library"
routes use: the entry lands in `<music>/.mlo/trash/<scope>/`, its origin is
recorded in the manifest beside it, and the Trash page lists it and can put it
back. `layout_apply` OFF still makes the whole pass a report, and the panel says
so on every row before you press anything.

## Two things it will not touch, and why

- A **foreign folder that HOLDS AUDIO** in the music root: nothing in the app
  can say where its contents belong, so it stays reported. (A foreign folder
  holding only loose files has nothing to lose, which is why that one goes.)
- A **hidden folder** — `.stfolder`, `.git`, whatever a sync client or a
  checkout marks its territory with. It is reported, never moved.

## The reason is asked again at the move

A report is a claim about the folder, and the folder can change between the scan
and the apply. Every removal re-derives its own reason from what is on disk at
that moment (`mlo.layout._may_trash`): a folder that gained audio, a stray that
became a sidecar, a path that is not inside the music folder, `.mlo` itself —
all refused, with the reason in the report. The panel's route re-scans first; a
runner passes the report it just built, which is exactly what the guard is for.
Run against a stale report whose folder has since been filled, the removal
fails with "it holds audio now" and the album stays where the user put it.

## It runs with every import, scoped to the album

Script 20 is in the import chain (after beets (14) has put the folder in its
canonical place, before the grade (4) reads it), and a run handed `targets` is
confined to those albums — so an import optimizes the album it just wrote and
nothing else, and stores no report (a partial scan must never become "the last
scan" the Library page warns from). A library-wide *Run All* still gets the
whole-folder pass and the stored report.

Measured in `tools/test_layout_case.py`: a scoped run over one album removes
that album's junk to the Trash, and a same-named junk file in another artist's
album is byte for byte identical afterwards.

## Where you see it

- **Optimization → Library layout** — the caption and the Apply button now say
  what will be removed and where it goes; the rows say it per row.
- **Settings → Library → Optimize the library layout when it is scanned
  (script 20)** (`layout_apply`) — the help lists exactly what the pass settles,
  including what it sends to the Trash.
- The script is called **Optimize library layout** in Run All, the script menu
  and the CLI. The id is still 20, so saved chains and force flags are untouched.

## Upgrading

Nothing to do: no key, no migration. The behaviour change is the point of the
release — junk in an album now goes to the Trash on the next import or run
instead of being reported forever — and it is reversible twice over: `layout_apply`
OFF reports without touching anything, and the Trash page restores anything the
pass removed.
