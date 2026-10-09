import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

/** List the app SHELL for the service worker.
 *
 *  The shell is what a cold start needs to paint: the entry chunk and
 *  everything it imports STATICALLY, the stylesheets, and the fonts those
 *  stylesheets load. The document names most of that list itself (the worker
 *  reads index.html too), but not a font — a `public/` font is referenced by
 *  the CSS, by URL — so the build is the only place that knows it.
 *
 *  The LAZY route chunks are deliberately NOT in it: they are the bulk of the
 *  build (the entry's closure is ~1.2 MB, every route added ~0.8 MB on top),
 *  and downloading them at activation competed with the first paint for code
 *  the user had not asked for. They arrive on first use instead, through the
 *  worker's own cache-first static path (`STATIC_RE` in web/public/sw.js),
 *  which stores whatever the browser fetches — so every page the user HAS
 *  opened stays offline-safe, and a page they never opened costs nothing. */
function precacheManifest(): Plugin {
  return {
    name: 'mlo-precache-manifest',
    apply: 'build',
    generateBundle(_options, bundle) {
      const shell = new Set<string>()
      // An entry chunk and its STATIC closure, followed recursively:
      // `dynamicImports` are the lazy routes, which is exactly what this list
      // must not carry, so they are never followed.
      const addChunk = (file: string) => {
        const out = bundle[file]
        if (!out || out.type !== 'chunk' || shell.has(file)) return
        shell.add(`/${file}`)
        for (const dep of out.imports) addChunk(dep)
      }
      for (const [file, out] of Object.entries(bundle)) {
        if (out.type === 'chunk' && out.isEntry) addChunk(file)
      }
      for (const [file, out] of Object.entries(bundle)) {
        if (out.type !== 'asset') continue
        // A font that went through the bundle is part of the shell too.
        if (/\.(?:woff2?|ttf|otf)$/.test(file)) shell.add(`/${file}`)
        if (!file.endsWith('.css')) continue
        shell.add(`/${file}`)
        // Vite leaves a public/ font's URL as written (`/fonts/<name>.woff2`),
        // so the stylesheet is where the shell's font is named.
        if (typeof out.source !== 'string') continue
        for (const m of out.source.matchAll(/url\(["']?(\/fonts\/[^"')]+)["']?\)/g)) shell.add(m[1])
      }
      this.emitFile({ type: 'asset', fileName: 'precache.json', source: JSON.stringify([...shell].sort()) })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), precacheManifest()],
  // The proxy target is the scratch server the dev session drives, not a
  // hardcoded 8000: that port is the owner's live install, and a dev server
  // proxying to it would let a click in a test bed hit the real library.
  // `MLO_API_TARGET=http://127.0.0.1:8011 MLO_WEB_PORT=5181 npm run dev`.
  server: {
    port: Number(process.env.MLO_WEB_PORT || 5173),
    proxy: {
      "/api": process.env.MLO_API_TARGET || "http://127.0.0.1:8011",
      "/ws": { target: (process.env.MLO_API_TARGET || "http://127.0.0.1:8011").replace(/^http/, "ws"), ws: true },
    },
  },
})
