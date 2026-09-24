#!/usr/bin/env node
/* The Import tab's selection: archives, folders (nested), individual files.
 *
 * The owner's ask, pinned the only way a drop can be: by driving the REAL page
 * with a REAL DataTransfer whose items answer `webkitGetAsEntry` the way a
 * browser's own do, and reading what the wizard then shows.
 *
 *   * the file picker accepts archives (`accept` names them) and picks them;
 *   * a dropped folder yields its audio, nested folders included (a folder
 *     holding an album folder must NOT collapse into one album);
 *   * a dropped archive is unpacked by the server and yields the SAME album set
 *     the same content gives as a folder — the acceptance question;
 *   * an archive with no audio says so instead of offering an empty album, and
 *     a crafted archive says why it was refused;
 *   * a single file picked, several picked, and a mixed drop (a folder AND an
 *     archive in one DataTransfer) each state what they took in;
 *   * committing an archive's album through the button really creates it.
 *
 * Needs a live backend serving the built app (`web/dist`), a scratch music
 * folder, and `first_run_done` already flipped:
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=F:/tmp/mlo-archives \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   curl -X POST http://127.0.0.1:8011/api/config -H "Content-Type: application/json" \
 *        -d '{"first_run_done": true}'
 *   PLAYWRIGHT=web/node_modules/playwright \
 *     node tools/check_import_archives.cjs http://127.0.0.1:8011
 *
 * Never point it at port 8000 (the owner's own instance — see AGENTS.md).
 * Exit codes: 0 pass, 1 a check failed, 2 the environment cannot run it. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[import-archives] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const zlib = require("node:zlib");

const BASE = process.argv.slice(2).find((a) => !a.startsWith("--")) ||
  process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(__dirname, "..", ".pi", "shots-import-archives");

let fail = 0;
let checks = 0;
const check = (label, ok, detail = "") => {
  checks += 1;
  if (!ok) fail += 1;
  console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${ok || !detail ? "" : ` — ${detail}`}`);
};

/* ------------------------------------------------------------------ *
 * fixtures: a tiny WAV, and zip/tar writers (stored entries only, so
 * this check needs no dependency beyond node's own zlib for the CRC)
 * ------------------------------------------------------------------ */
function wav(seconds) {
  const frames = Math.max(1, Math.round(44100 * seconds));
  const data = Buffer.alloc(frames * 4);            // silence, 16-bit stereo
  const head = Buffer.alloc(44);
  head.write("RIFF", 0);
  head.writeUInt32LE(36 + data.length, 4);
  head.write("WAVEfmt ", 8);
  head.writeUInt32LE(16, 16);
  head.writeUInt16LE(1, 20);
  head.writeUInt16LE(2, 22);
  head.writeUInt32LE(44100, 24);
  head.writeUInt32LE(44100 * 4, 28);
  head.writeUInt16LE(4, 32);
  head.writeUInt16LE(16, 34);
  head.write("data", 36);
  head.writeUInt32LE(data.length, 40);
  return Buffer.concat([head, data]);
}

/** A stored-entry zip — the member names are exactly what is passed. */
function zip(entries) {
  const locals = [];
  const central = [];
  let offset = 0;
  for (const [name, value] of entries) {
    const data = Buffer.isBuffer(value) ? value : Buffer.from(value);
    const nameBuf = Buffer.from(name, "utf8");
    const crc = zlib.crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(0, 8);                      // stored
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    locals.push(local, nameBuf, data);

    const dir = Buffer.alloc(46);
    dir.writeUInt32LE(0x02014b50, 0);
    dir.writeUInt16LE(20, 4);
    dir.writeUInt16LE(20, 6);
    dir.writeUInt16LE(0, 10);                       // stored
    dir.writeUInt32LE(crc, 16);
    dir.writeUInt32LE(data.length, 20);
    dir.writeUInt32LE(data.length, 24);
    dir.writeUInt16LE(nameBuf.length, 28);
    dir.writeUInt32LE(offset, 42);
    central.push(dir, nameBuf);
    offset += 30 + nameBuf.length + data.length;
  }
  const body = Buffer.concat([...locals, ...central]);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(Buffer.concat(central).length, 12);
  end.writeUInt32LE(Buffer.concat(locals).length, 16);
  return Buffer.concat([body, end]);
}

const CUE = ["01 - One.wav", "02 - Two.wav", "03 - Three.wav", "04 - Four.wav"]
  .map((n, i) => `FILE "${n}" WAVE\n  TRACK ${String(i + 1).padStart(2, "0")} AUDIO\n` +
    `    TITLE "Track ${i + 1}"\n    PERFORMER "The Artist"`)
  .join("\n");

const TREE = {
  "01 - One.wav": wav(0.05),
  "02 - Two.wav": wav(0.05),
  "cover.jpg": Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a]),
  "rip.cue": CUE,
};

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "mlo-chk-archives-"));
const ARCH = path.join(TMP, "The Album.zip");
fs.writeFileSync(ARCH, zip(Object.entries(TREE)));
const SCANS = path.join(TMP, "scans.zip");
fs.writeFileSync(SCANS, zip([["cover.jpg", TREE["cover.jpg"]], ["notes.txt", "nothing"]]));
const BAD = path.join(TMP, "crafted.zip");
fs.writeFileSync(BAD, zip([["../../escape.wav", wav(0.02)]]));
const SINGLE = path.join(TMP, "solo.flac");
fs.writeFileSync(SINGLE, Buffer.concat([Buffer.from("fLaC"), Buffer.alloc(4096)]));

/* ------------------------------------------------------------------ *
 * the page
 * ------------------------------------------------------------------ */
const WAV_B64 = {};

async function main() {
  const alive = await fetch(`${BASE}/api/config`).then((r) => r.ok).catch(() => false);
  if (!alive) {
    console.error(`[import-archives] no backend at ${BASE} — start one (see the header).`);
    process.exit(2);
  }
  fs.mkdirSync(SHOTS, { recursive: true });

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const dropZone = async () =>
    page.evaluate(`(() => {
      const el = [...document.querySelectorAll("div")].find((d) =>
        (d.className || "").includes("panel-hero"));
      return !!el;
    })()`);

  /** Drop `items` on the wizard's drop target, as a real drag would: the event
   *  carries a DataTransfer whose `items` answer `webkitGetAsEntry` (a
   *  directory reader for a folder, a File for a file) — the exact API the
   *  wizard's own traversal reads. */
  /** Drop `items` on the wizard's drop target the way Playwright's own
   *  dispatch does it: the DataTransfer is REAL and built inside the page, and
   *  its items answer `webkitGetAsEntry` (a directory reader for a folder, a
   *  File for a file) — the exact API the wizard's traversal reads. */
  const drop = async (items) => {
    const payload = items.map((it) => it.kind === "dir"
      ? { kind: "dir", name: it.name, entries: it.entries.map((e) => e.kind === "dir"
        ? { kind: "dir", name: e.name, entries: e.entries }
        : { kind: "file", name: e.name, data: e.data }) }
      : { kind: "file", name: it.name, data: it.data });
    const dt = await page.evaluateHandle(`(() => {
      const payload = ${JSON.stringify(payload)};
      const file = (name, bytes) => new File([new Uint8Array(bytes)], name);
      const toEntry = (spec) => spec.kind === "file"
        ? { isFile: true, isDirectory: false, name: spec.name,
            file: (ok) => ok(file(spec.name, spec.data)) }
        : { isFile: false, isDirectory: true, name: spec.name,
            createReader: () => {
              let sent = false;
              return { readEntries: (ok) => {
                if (sent) return ok([]);
                sent = true;
                return ok(spec.entries.map(toEntry));
              } };
            } };
      const dt = new DataTransfer();
      const roots = payload.map(toEntry);
      Object.defineProperty(dt, "items", {
        value: roots.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry })),
      });
      return dt;
    })()`);
    await page.dispatchEvent(".panel-hero", "drop", { dataTransfer: dt });
    await dt.dispose();
  };

  /** The selection step's own words, as a reader sees them. */
  const panelText = () => page.evaluate(
    `document.body.innerText.replace(/\\s+/g, " ").trim()`);
  const albumNames = () => page.evaluate(`(() =>
    [...document.querySelectorAll(".panel input.input")].map((i) => i.value)
      .filter(Boolean).sort())()`);

  await page.goto(`${BASE}/import`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".panel-hero", { timeout: 20000 });
  check("the import tab renders its drop zone", await dropZone());

  // ---- 1. the picker offers archives -------------------------------------
  const accept = await page.getAttribute("#import-files", "accept");
  for (const ext of [".zip", ".7z", ".rar", ".tar.gz", ".flac", ".cue"]) {
    check(`the file picker accepts ${ext}`, (accept || "").includes(ext), accept || "no accept=");
  }
  check("the picker takes several files at once",
    (await page.getAttribute("#import-files", "multiple")) !== null);

  // ---- 2. a single individual file ---------------------------------------
  await page.setInputFiles("#import-files", SINGLE);
  await page.waitForFunction(`document.body.innerText.includes("Separate into albums")`,
    null, { timeout: 20000 }).catch(() => {});
  let text = await panelText();
  check("a single picked file is a one-file selection",
    /1 file\(s\)/.test(text) && /1 album\(s\)/.test(text),
    text.slice(0, 240));
  check("…named after the file it was", text.includes("solo.flac"), text.slice(0, 240));
  await page.screenshot({ path: path.join(SHOTS, "01-single-file.png") });

  // ---- 3. several individual files at once -------------------------------
  const two = [path.join(TMP, "a.flac"), path.join(TMP, "b.flac")];
  for (const f of two) fs.copyFileSync(SINGLE, f);
  await page.setInputFiles("#import-files", two);
  await page.waitForFunction(`document.body.innerText.includes("2 file(s)")`,
    null, { timeout: 20000 }).catch(() => {});
  text = await panelText();
  check("several picked files are counted as they arrived",
    /2 file\(s\)/.test(text), text.slice(0, 240));

  // ---- 4. a FOLDER dropped, nested levels included -----------------------
  const folderDrop = [{ kind: "dir", name: "Rips", entries: [
    { kind: "dir", name: "The Album", entries: [
      { kind: "file", name: "01 - One.wav", data: [...TREE["01 - One.wav"]] },
      { kind: "file", name: "02 - Two.wav", data: [...TREE["02 - Two.wav"]] },
      { kind: "file", name: "rip.cue", data: [...Buffer.from(CUE)] },
    ] },
    { kind: "dir", name: "Another", entries: [
      { kind: "file", name: "01 - Solo.wav", data: [...wav(0.03)] },
    ] },
  ] }];
  await drop(folderDrop);
  await page.waitForFunction(`document.body.innerText.includes("The Album")`,
    null, { timeout: 20000 }).catch(() => {});
  const folderText = await panelText();
  check("a dropped folder yields its audio",
    JSON.stringify(await albumNames()) === JSON.stringify(["Another", "The Album"]),
    JSON.stringify(await albumNames()));
  check("…and a folder of album folders stays SEPARATE albums",
    /4 file\(s\) → 2 album\(s\)/.test(folderText),
    folderText.slice(0, 300));
  check("…with each file under its own folder",
    folderText.includes("Rips/The Album/01 - One.wav"), folderText.slice(0, 400));
  await page.screenshot({ path: path.join(SHOTS, "02-folder-drop.png") });

  // The SAME album content as a plain folder — what the archive must match.
  const contents = [{ kind: "dir", name: "The Album", entries: [
    { kind: "file", name: "01 - One.wav", data: [...TREE["01 - One.wav"]] },
    { kind: "file", name: "02 - Two.wav", data: [...TREE["02 - Two.wav"]] },
    { kind: "file", name: "cover.jpg", data: [...TREE["cover.jpg"]] },
    { kind: "file", name: "rip.cue", data: [...Buffer.from(CUE)] },
  ] }];
  await drop(contents);
  await page.waitForFunction(`document.body.innerText.includes("4 file(s)")`,
    null, { timeout: 20000 }).catch(() => {});
  const folderAlbums = await albumNames();
  const folderFiles = await page.evaluate(`(() =>
    [...document.querySelectorAll(".panel .truncate, .panel .flex-1")]
      .map((n) => n.textContent.trim())
      .filter((t) => /\\.(wav|flac|cue|jpg)$/.test(t)).sort())()`);
  check("the folder gives the album set the archive is compared against",
    JSON.stringify(folderAlbums) === JSON.stringify(["The Album"]),
    JSON.stringify(folderAlbums));

  // ---- 5. an ARCHIVE dropped: the same album set as the folder -----------
  await drop([{ kind: "file", name: "The Album.zip", data: [...fs.readFileSync(ARCH)] }]);
  await page.waitForFunction(`document.body.innerText.includes("Archives unpacked")`,
    null, { timeout: 30000 }).catch(() => {});
  const archiveText = await panelText();
  check("a dropped archive is unpacked and says so",
    archiveText.includes("The Album.zip") && archiveText.includes("Archives unpacked"),
    archiveText.slice(0, 300));
  check("…with the counts the server found",
    /The Album\.zip — 4 files unpacked, 2 with audio/.test(archiveText),
    archiveText.slice(0, 300));
  const archiveAlbums = await albumNames();
  const archiveFiles = await page.evaluate(`(() =>
    [...document.querySelectorAll(".panel .truncate, .panel .flex-1")]
      .map((n) => n.textContent.trim())
      .filter((t) => /\\.(wav|flac|cue|jpg)$/.test(t)).sort())()`);
  check("the archive offers the SAME album set the folder did",
    JSON.stringify(archiveAlbums) === JSON.stringify(["The Album"]),
    JSON.stringify(archiveAlbums));
  check("…and the same files, under the archive's own name",
    JSON.stringify(archiveFiles) === JSON.stringify(folderFiles),
    `${JSON.stringify(archiveFiles)} vs ${JSON.stringify(folderFiles)}`);
  await page.screenshot({ path: path.join(SHOTS, "03-archive-drop.png") });

  // ---- 6. committing it really creates the album -------------------------
  await page.click("button.btn-primary:has-text('Import 1 album')");
  await page.waitForFunction(`document.body.innerText.includes("Links")`,
    null, { timeout: 60000 }).catch(() => {});
  const staged = await fetch(`${BASE}/api/library`).then((r) => r.json());
  const albums = (staged.artists || []).flatMap((a) => (a.albums || []).map((x) => x.path));
  const made = albums.find((p) => /The Album/i.test(p));
  check("pressing Import really lands the archive's album in the library",
    !!made, JSON.stringify(albums));
  if (made) {
    const lib = (staged.artists || []).flatMap((a) => a.albums || [])
      .find((x) => x.path === made);
    const files = ((lib && lib.tracks) || []).map((t) => t.file).sort();
    // The sheets themselves (the .cue/.log travelling with the tracks) are
    // pinned on disk by tools/test_import_archives.py — the library payload
    // lists tracks, and a sidecar is not a track.
    check("…with the archive's own tracks in it",
      JSON.stringify(files) === JSON.stringify(["01 - One.wav", "02 - Two.wav"]),
      JSON.stringify(files));
  }
  await page.screenshot({ path: path.join(SHOTS, "04-committed.png") });

  // ---- 7. an archive with no audio, and a refused one --------------------
  await page.goto(`${BASE}/import`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".panel-hero", { timeout: 20000 });
  await page.setInputFiles("#import-files", SCANS);
  await page.waitForFunction(`document.body.innerText.includes("none of them audio")`,
    null, { timeout: 30000 }).catch(() => {});
  text = await panelText();
  check("an archive with no audio says so instead of offering an empty album",
    text.includes("none of them audio") && text.includes("nothing was added"),
    text.slice(0, 300));
  check("…and no album is offered for it",
    !text.includes("Separate into albums"), text.slice(0, 200));
  await page.screenshot({ path: path.join(SHOTS, "05-no-audio.png") });

  await page.setInputFiles("#import-files", BAD);
  await page.waitForFunction(`document.body.innerText.includes("refused")`,
    null, { timeout: 30000 }).catch(() => {});
  text = await panelText();
  check("a crafted archive is refused, with the member named",
    text.includes("refused") && text.includes("escape.wav"), text.slice(0, 300));
  await page.screenshot({ path: path.join(SHOTS, "06-refused.png") });

  // ---- 8. a mixed drop: a folder AND an archive in one DataTransfer ------
  await page.goto(`${BASE}/import`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".panel-hero", { timeout: 20000 });
  await drop([
    { kind: "dir", name: "Loose Folder", entries: [
      { kind: "file", name: "01 - Loose.wav", data: [...wav(0.04)] },
    ] },
    { kind: "file", name: "The Album.zip", data: [...fs.readFileSync(ARCH)] },
  ]);
  await page.waitForFunction(`document.body.innerText.includes("Archives unpacked")`,
    null, { timeout: 30000 }).catch(() => {});
  text = await panelText();
  const mixedAlbums = await albumNames();
  check("a drop of a folder AND an archive keeps both, as separate albums",
    JSON.stringify(mixedAlbums) === JSON.stringify(["Loose Folder", "The Album"]),
    JSON.stringify(mixedAlbums));
  check("…and states what it took in",
    /5 file\(s\) → 2 album\(s\)/.test(text), text.slice(0, 300));
  await page.screenshot({ path: path.join(SHOTS, "07-mixed-drop.png") });

  // ---- 9. the desktop shell's own way in: a path the server reads --------
  const byPath = await fetch(
    `${BASE}/api/import/unpack?path=${encodeURIComponent(ARCH)}`, { method: "POST" })
    .then((r) => r.json()).catch(() => null);
  check("an archive can be unpacked from a server-side path (the desktop drop)",
    !!byPath && byPath.audio === 2 && byPath.unpacked === 4,
    JSON.stringify(byPath).slice(0, 240));
  if (byPath && byPath.dir) {
    await fetch(`${BASE}/api/import/unpack/discard`, {
      method: "POST",
      body: new URLSearchParams({ dirs: byPath.dir }),
    });
  }

  await browser.close();
  fs.rmSync(TMP, { recursive: true, force: true });
}

main()
  .catch((e) => {
    console.error(`[import-archives] the check could not run: ${e}`);
    process.exit(2);
  })
  .finally(() => fs.rmSync(TMP, { recursive: true, force: true }))
  .then(() => {
    if (fail) {
      console.error(`[import-archives] FAILED (${fail} of ${checks})`);
      process.exit(1);
    }
    console.log(`ok  the Import tab takes archives, folders (nested) and individual ` +
      `files — by pick and by drop — unpacks an archive into the same album set the ` +
      `folder gives, and says what it took in (${checks} checks)`);
  });
