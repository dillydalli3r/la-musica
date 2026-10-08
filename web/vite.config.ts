import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
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
