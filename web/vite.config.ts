import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

/** List every built chunk for the service worker.
 *
 *  The lazy route chunks are not referenced by index.html, so a precache built
 *  from the document alone leaves an offline navigation to a page you have not
 *  visited online hanging on its Suspense fallback forever. The build knows
 *  the names; emit them as a plain list the worker fetches on activation. */
function precacheManifest(): Plugin {
  return {
    name: 'mlo-precache-manifest',
    apply: 'build',
    generateBundle(_options, bundle) {
      const files = Object.keys(bundle)
        .filter((f) => /\.(?:js|css|woff2?)$/.test(f))
        .map((f) => `/${f}`)
      this.emitFile({ type: 'asset', fileName: 'precache.json', source: JSON.stringify(files) })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), precacheManifest()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000", "/ws": { target: "ws://127.0.0.1:8000", ws: true } } },
})
