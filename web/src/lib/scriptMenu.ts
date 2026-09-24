/** The generated script entries of the details menu — the registry's answer,
 *  turned into menu lines.
 *
 *  No script id is typed here: `GET /api/script-menu` carries every script with
 *  the entity kinds it applies to and what its runner should be handed
 *  (`server/script_menu.py` derives both from the runners' own code). This
 *  module only decides WHICH of them the entity in front of the user gets, and
 *  on WHAT:
 *
 *    * a FILE-scoped script (its work unit is one file) runs on the selection's
 *      own paths;
 *    * a FOLDER-scoped script (sidecars, cover art, a per-album measurement,
 *      the release manifest, the artist image, a subtree's layout) runs on the
 *      folder the selection sits in — the album's, the artist's, or the folders
 *      the selected files live in — and is offered only where such a folder is
 *      in hand, which is what `applies_to` states.
 *
 *  Kept apart from the component so `tools/check_script_menu.mjs` can pin the
 *  answer for an album and for a track row without a browser. */
import type { EntityKind, ScriptMenu, ScriptMenuScript } from "../types";

/** One line of the menu: a script to run over the current selection, or its
 *  forced twin. */
export interface ScriptEntry {
  id: number;
  /** "6 · Audit library" — the stack's own way of naming a script, and the
   *  order the Run All chain runs it in. */
  label: string;
  /** What the script does, plus the reason it cannot run when it cannot. */
  title: string;
  /** The paths /api/run is given: the selection, or the folder it sits in. */
  targets: string[];
  /** The one force key a forced twin sends; absent on a plain run. */
  force?: string;
  /** Shown but not pressable: no such runner in this install, or the script's
   *  feature is switched off (the title says which, and why). */
  disabled: boolean;
}

export interface ScriptSection {
  id: string;
  title: string;
  entries: ScriptEntry[];
}

export interface ScriptSections {
  /** One section per group the stack shows scripts in, in its own order. */
  groups: ScriptSection[];
  /** The forced re-runs, in the same order — the variant of the entries above,
   *  pressed only when the user asks for the work to be redone. */
  forced: ScriptSection | null;
}

/** What a menu was mounted with. */
export interface MenuContext {
  /** The tracks/albums the actions apply to. */
  paths: string[];
  /** Album folder, when the menu is the album's. */
  albumPath?: string;
  /** Artist folder, when the menu is the artist's (`_artist_folder_of` needs a
   *  real path; an album folder works too, so this is a refinement, not a
   *  requirement). */
  artistPath?: string;
}

/** The entity a menu is on: what was passed, else what the mount implies. A
 *  caller that knows better (the track page holds an album folder for its own
 *  credits panel) says so; otherwise a folder means the album, an artist name
 *  means the artist, one path means the track row, and anything else is a
 *  path list. */
export function entityKind(props: {
  kind?: EntityKind;
  albumPath?: string;
  artist?: string;
  paths: string[];
}): EntityKind {
  if (props.kind) return props.kind;
  if (props.albumPath) return "album";
  if (props.artist) return "artist";
  if (props.paths.length === 1) return "track";
  return "library";
}

/** The folder a path sits in, for the folder-scoped scripts. Both separators:
 *  a library is browsed from a Linux server and mounted on Windows. */
export function folderOf(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut > 0 ? path.slice(0, cut) : path;
}

/** The folders a selection sits in, deduplicated (case-insensitively, as the
 *  rest of the app compares library paths). */
function foldersOf(paths: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const p of paths) {
    const dir = folderOf(p);
    const key = dir.toLowerCase();
    if (!dir || seen.has(key)) continue;
    seen.add(key);
    out.push(dir);
  }
  return out;
}

/** What a script of *kind* is handed: the selection's files, or the folder the
 *  menu is for. */
export function targetsFor(script: ScriptMenuScript, kind: EntityKind, ctx: MenuContext): string[] {
  if (script.scope !== "folder") return ctx.paths;
  // The menu's own folder first (an album's, an artist's), because that is the
  // whole entity — a folder target also picks up sidecars no selected file
  // names. Without one, the folders the selection sits in are what a folder
  // run can be scoped to (the runners derive their albums from files this way).
  if (kind === "album" && ctx.albumPath) return [ctx.albumPath];
  if (kind === "artist" && ctx.artistPath) return [ctx.artistPath];
  return foldersOf(ctx.paths);
}

function entry(script: ScriptMenuScript, kind: EntityKind, ctx: MenuContext, force?: string): ScriptEntry {
  // The stack page names a script "6 · Audit library"; the same here, so a
  // number in the menu and a number in the report are the same script.
  const label = `${script.id} · ${script.label}`;
  // Shown, never silently dropped: a script the install cannot run, or whose
  // feature is switched off, is still an entry — with the reason on it. The
  // gate sentence is the run's OWN ("mood_enabled is off"), so the tooltip and
  // the run report cannot disagree.
  const why = !script.available ? "not installed" : script.gate.reason;
  return {
    id: script.id,
    label,
    title: why ? `${script.description} — ${why}` : script.description,
    targets: targetsFor(script, kind, ctx),
    ...(force ? { force } : {}),
    disabled: !script.available || !script.gate.enabled,
  };
}

/** The generated sections for *kind*, in the stack's order.
 *
 *  A script that applies to this entity appears exactly ONCE as a plain run,
 *  and — where it owns a single force flag — once more as its forced twin, so
 *  there is exactly one way to run a given script and one way to insist. */
export function scriptSections(
  menu: ScriptMenu | null | undefined,
  kind: EntityKind,
  ctx: MenuContext
): ScriptSections {
  const groups: ScriptSection[] = [];
  if (!menu) return { groups, forced: null };
  const mine = menu.scripts.filter((s) => s.applies_to.includes(kind));
  // The payload's groups are the stack's (one list for scripts — api_stack
  // groups CHECKS, its scripts are the Run All chain), so a group with nothing
  // to offer this entity is left out rather than shown empty.
  for (const g of menu.groups) {
    const entries = mine.filter((s) => s.group === g.id).map((s) => entry(s, kind, ctx));
    if (entries.length) groups.push({ id: g.id, title: g.title, entries });
  }
  const forcedEntries = mine
    .filter((s) => s.force.key)
    .map((s) => entry(s, kind, ctx, s.force.key ?? undefined));
  return {
    groups,
    forced: forcedEntries.length
      ? { id: menu.forced_group.id, title: menu.forced_group.title, entries: forcedEntries }
      : null,
  };
}
