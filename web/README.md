# la musica — web UI

React 19 + TypeScript + Tailwind, built by Vite. The dev server proxies `/api`
(and the websocket) to the FastAPI backend on `127.0.0.1:8000`; the production
build in `dist/` is served by the backend itself.

## Development

```bash
npm install
npm run dev      # http://localhost:5173, proxies /api to :8000
npm run build    # type-check (tsc -b) + production build into dist/
npm run lint     # oxlint
```

The backend must be running for anything to load: `docker compose up -d` in the
repository root (see the main README), or `python -m server.main` from a source
checkout.

## Layout

| Path | What lives there |
| --- | --- |
| `src/pages/` | one file per route (Library, Album, Artist, Track, Player views, Export, Grading, Dependencies, …) |
| `src/components/` | shared UI: player bar, cover art, modals, tables, progress |
| `src/lib/` | framework-free helpers the checks below import (`status.ts` grade verdicts, `lyrScroll.ts` lyrics auto-scroll, `scripts.ts` script labels, `fmt.ts`) |
| `src/api.ts` | the typed API surface — one function per endpoint, with its payload types |
| `src/store.ts` | the small Zustand store (player, toasts, offline cache) |
| `public/` | static assets: fonts, icons, `sw.js` (the service worker, precaching `precache.json` from the build) |

## Checks

`npm run build` type-checks the whole app. The behavioural checks that need a
DOM live in the repository's `tools/` folder and run with plain Node (they
import the `src/lib` modules directly, so they stay fast and framework-free):

```bash
node ../tools/check_lyrscroll.cjs        # lyrics pane auto-scroll
node ../tools/check_player_state.cjs     # player state machine
node ../tools/check_sidebar.cjs          # sidebar routing/labels
node ../tools/check_trash.cjs            # trash views
node ../tools/check_menus.cjs            # script menu lists (needs the backend)
```

CI (`.github/workflows/ci.yml`) runs `tsc -b`, `oxlint`, `npm run build` and
`check_lyrscroll.cjs` on every push.
