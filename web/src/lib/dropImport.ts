/** App-wide drag & drop into the Import wizard.
 *
 *  A drop anywhere in the app is an import: the user should not have to find
 *  the wizard first. The browser's File/Entry API only answers DURING the
 *  drop event (`DataTransferItem.getAsFile` and `webkitGetAsEntry` go stale
 *  afterwards), so the entries are walked to plain `File` objects here — the
 *  event's own turn — and those together with the desktop shell's dropped
 *  PATHS are what the wizard later consumes from this module.
 *
 *  The wizard still owns what an import IS (album detection, archives, the
 *  staged queue); this module only carries the drop across a route change.
 */
export type PendingDrop =
  | { kind: "files"; files: File[] }
  | { kind: "paths"; paths: string[] };

export const PENDING_IMPORT_EVENT = "mlo:pending-import";
let pending: PendingDrop | null = null;

export function setPendingImport(drop: PendingDrop) {
  pending = drop;
  window.dispatchEvent(new Event(PENDING_IMPORT_EVENT));
}

/** The drop waiting for the wizard, if any (one-shot). */
export function takePendingImport(): PendingDrop | null {
  const out = pending;
  pending = null;
  return out;
}

type DirEntry = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  file?: (ok: (f: File) => void, fail: (e: unknown) => void) => void;
  createReader?: () => {
    readEntries: (ok: (rows: DirEntry[]) => void, fail: (e: unknown) => void) => void;
  };
};

/** A dropped folder arrives as a tree; every file keeps its path inside the
 *  drop as `webkitRelativePath` (the wizard groups albums by it, exactly as a
 *  folder input does). */
async function walk(entry: DirEntry, prefix: string, out: File[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => {
      entry.file?.(resolve, reject);
    });
    const rel = prefix ? `${prefix}/${file.name}` : file.name;
    try {
      Object.defineProperty(file, "webkitRelativePath", { value: rel, configurable: true });
    } catch {
      /* a File that refuses the property still imports by its own name */
    }
    out.push(file);
    return;
  }
  if (!entry.isDirectory || !entry.createReader) return;
  const here = prefix ? `${prefix}/${entry.name}` : entry.name;
  const reader = entry.createReader();
  // `readEntries` answers in batches (often 100 at a time) until one is empty.
  for (;;) {
    const batch = await new Promise<DirEntry[]>((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (!batch.length) break;
    for (const child of batch) await walk(child, here, out);
  }
}

/** Every file a DOM drop holds — files, nested folders and archives alike. */
export async function filesFromDrop(dt: DataTransfer): Promise<File[]> {
  const items = Array.from(dt.items ?? []);
  const roots: DirEntry[] = [];
  for (const item of items) {
    if (item.kind !== "file") continue;
    const entry = (item as DataTransferItem & { webkitGetAsEntry?: () => DirEntry | null })
      .webkitGetAsEntry?.();
    if (entry) roots.push(entry);
  }
  if (!roots.length) return Array.from(dt.files ?? []);
  const out: File[] = [];
  for (const root of roots) await walk(root, "", out);
  return out;
}