# TCG Desktop (Tauri v2 wrapper)

An **additive** desktop wrapper around the existing app. It changes nothing about
the web path: the same FastAPI backend and Vite/React frontend keep running via
`./start.sh`. This `desktop/` directory is the only new surface — it bundles the
backend as a standalone **sidecar** and shows the existing frontend in a native
window, for Windows / macOS / Linux binaries.

> Target audience: machines that are already IP-allowlisted on the dwh database
> (the backend connects directly to dwh, exactly like the web path). These are
> **not** general-distribution binaries — see *Data / credentials* below.

## Layout
```
desktop/
  sidecar/            PyInstaller build of the FastAPI backend (tcg.core)
    tcg_backend.py      entry (loads .env, runs uvicorn on the app)
    tcg-backend.spec    PyInstaller one-file spec (bundles numpy/scipy/psycopg)
    build_sidecar.sh    build + stage the sidecar for Tauri's externalBin
  src-tauri/          Tauri v2 crate (Rust)
    tauri.conf.json     window, CSP, externalBin, frontendDist=../../frontend/dist
    src/lib.rs          spawns + health-waits + lifecycle-manages the sidecar
    capabilities/       grants the shell plugin permission to run the sidecar
    binaries/           staged sidecar: tcg-backend-<target-triple>[.exe]
  package.json        Tauri CLI + dev/build scripts
../.github/workflows/desktop-build.yml   cross-OS build matrix (on-demand)
```

## Run the WEB app (unchanged)
From the repo root — nothing here affects it:
```bash
./start.sh          # backend (uvicorn :8000) + Vite (:5173)
```

## Run the DESKTOP app (dev)
Prerequisites (one-time):
- **Rust** (`https://rustup.rs`).
- **Linux system deps** (Ubuntu 22.04, matching CI):
  ```bash
  sudo apt install -y libwebkit2gtk-4.1-dev build-essential curl wget file \
    libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
  ```
  (macOS: Xcode CLT. Windows: WebView2 + MSVC build tools.)
- **uv** with the build extra: `uv sync --extra desktop` (adds PyInstaller).

Build the sidecar once (re-run after backend changes), then start dev:
```bash
desktop/sidecar/build_sidecar.sh        # → desktop/src-tauri/binaries/tcg-backend-<triple>
cd desktop && npm install && npm run dev # = tauri dev (Vite + app + sidecar)
```
`tauri dev` loads the Vite dev server (`http://localhost:5173`); the Rust layer
spawns the sidecar on `127.0.0.1:8000` and kills it on exit.

## Build a DESKTOP binary
Local (Linux), no bundling:
```bash
desktop/sidecar/build_sidecar.sh
cd desktop && npm run build -- --no-bundle   # → src-tauri/target/release/tcg-desktop
```
Local Linux **bundles** (`.deb` / `.AppImage`):
```bash
cd desktop && npm run build                  # → src-tauri/target/release/bundle/
```
**Windows / macOS** binaries must be built on their own OS — use the GitHub
Actions matrix at `.github/workflows/desktop-build.yml` (run it from the Actions
tab / `workflow_dispatch`, or push a `desktop-v*` tag). It builds the per-OS
sidecar, stages it, and runs `tauri build`, uploading the artifacts. The produced
macOS/Windows binaries are **unsigned** (code signing/notarization needs certs);
on macOS, clear the quarantine flag after download with
`xattr -d com.apple.quarantine "Trajectoire CAP.app"` if Gatekeeper blocks it.

## How it fits together
- **Backend = sidecar.** `tcg.core` is frozen by PyInstaller into a single
  executable (numpy/scipy/psycopg bundled). Tauri spawns it via the shell plugin
  (`externalBin`), so no Python/uv is needed at runtime.
- **Frontend = the existing build.** `frontendDist` points at `../../frontend/dist`;
  in dev the window uses the Vite dev server. The frontend is never forked.
- **CORS.** A packaged webview's origin is `tauri://localhost` on Linux/macOS
  and `http://tauri.localhost` on Windows (WebView2), not `:5173`. `src/lib.rs`
  spawns the sidecar with
  `TCG_CORS_ORIGINS=tauri://localhost,https://tauri.localhost,http://tauri.localhost,http://localhost:5173`
  so the backend accepts the webview's requests on every OS. (Omitting the
  Windows `http://tauri.localhost` is what made packaged Windows fail every
  fetch with "Backend unreachable".)

## Releasing / versioning
The shipped bundle version comes from **`src-tauri/tauri.conf.json`** and
**`src-tauri/Cargo.toml`** — bump BOTH (keep them equal) for a release, then push
a `desktop-v<version>` tag to trigger the cross-OS build. `package.json`'s
version is unused by the bundle (it is only the npm project for the Tauri CLI)
and is intentionally left at its initial value.

## Data / credentials
The sidecar reads `DWH_*` / `APP_DB_*` from a `.env` (same keys as the web path).
`src/lib.rs` resolves which `.env` to use and passes its absolute path to the
sidecar via `TCG_ENV_FILE`, searching (first hit wins): `TCG_ENV_FILE` if already
set → `<app config dir>/.env` (where the in-app **Settings → Database
connection** form writes them) → a `.env` next to the executable → the repo-root
`.env` (dev). The Rust layer never reads or logs the file's contents.

Credentials entered in Settings are stored **plaintext** in
`<app config dir>/.env` — the standard trade-off for a local desktop app with no
bundled secret store. They are user-scoped and, on Unix, written `0600`
(owner-only; the directory `0700`), so they are not world-readable on a shared
host. **No secrets are bundled into the binary or committed.** The backend fails
fast if dwh is unreachable, so the host must be on the dwh IP allowlist.

## Module boundaries
This wrapper lives entirely under `desktop/` (+ the one CI workflow). It does not
import or modify `tcg/`, `frontend/`, or `start.sh`; import-linter's 4 contracts
are unaffected (Tauri is outside the `tcg` Python package). The only change
outside `desktop/` is a `desktop` optional-dependency group in `pyproject.toml`
for the build-time PyInstaller dependency.
