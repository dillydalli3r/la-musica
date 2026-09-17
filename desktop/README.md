# la musica — Desktop shell

Tauri v2 (Rust) wrapper around the React UI and Python backend.

## How it works

- **Window** shows the built React app (`../web/dist`, built by
  `beforeBuildCommand`).
- **Backend**: on startup the shell spawns the FastAPI backend on
  `127.0.0.1:8000` and stops it when the app is quit. Resolution order:
  1. Bundled `mlo-server.exe` next to the app binary (PyInstaller one-file
     build — optional, for fully standalone installers)
  2. `python -m uvicorn server.main:app` from the repo checkout, when that
     checkout actually contains `server/main.py`
  Without either, the shell shows a "backend not found" dialog instead of
  spawning a `python` that has nothing to run.
- **Native folder picker**: `pick_folder` Tauri command, used by the import
  wizard via `invoke` to pick a source folder. The music folder itself is
  decided at startup (`MLO_MUSIC_FOLDER`, or `music_folder` in
  `config.json`) and is read-only in Settings — Settings has no picker.

## Development

```bash
cd desktop
npm install
npm run dev        # vite dev UI + tauri window, spawns backend
npm run build      # release bundle (NSIS/msi on Windows)
```

The web UI detects the Tauri webview (`window.__TAURI_INTERNALS__`) and
switches API calls to `http://127.0.0.1:8000` automatically.

## Standalone installers

To ship a fully standalone `.exe` without requiring Python:

1. Build the backend as a single file:
   `pyinstaller --onefile server/main.py --name mlo-server`
2. Place `mlo-server.exe` next to the built app binary (or into
   `src-tauri/resources/` with the Tauri sidecar mechanism).

Icons regenerate with `python tools/make_tauri_icons.py`.