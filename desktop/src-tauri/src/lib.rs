//! Tauri v2 desktop wrapper for Trajectoire CAP.
//!
//! This crate is intentionally ADDITIVE: it spawns the existing FastAPI backend
//! as a bundled PyInstaller *sidecar* (`binaries/tcg-backend-<target-triple>`)
//! and shows the existing Vite/React frontend in a single window. It forks
//! neither the backend (`tcg/`) nor the frontend (`frontend/`) nor `start.sh`.
//!
//! Lifecycle: the sidecar is spawned in `setup`, its child handle is kept in
//! managed state, and it is killed when the app exits so no orphaned backend
//! process is left behind.

use std::sync::Mutex;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Local address the sidecar binds to. Must match the args granted in
/// `capabilities/default.json` and the frontend's API base URL.
const SIDECAR_HOST: &str = "127.0.0.1";
const SIDECAR_PORT: u16 = 8000;

/// CORS origins the bundled webview is served from. In a packaged Tauri app the
/// document origin is `tauri://localhost` (Linux/Windows) or
/// `https://tauri.localhost`, NOT `http://localhost:5173`. The backend defaults
/// to allowing only the Vite dev origin, so without this the production webview's
/// fetches are blocked by CORS. We include the dev origin too so the same value
/// works under `tauri dev`.
const SIDECAR_CORS_ORIGINS: &str =
    "tauri://localhost,https://tauri.localhost,http://localhost:5173";

/// Holds the spawned sidecar's child handle so we can terminate it on exit.
/// `CommandChild::kill` consumes the handle, hence `Option` + `take()`.
struct SidecarProcess(Mutex<Option<CommandChild>>);

/// Resolve the absolute path to the repo checkout's gitignored `.env`.
///
/// Order:
///   1. `TCG_ENV_FILE` if already set in this process's environment (operator
///      override; passed straight through to the sidecar).
///   2. `<crate-dir>/../../.env` — i.e. the `TCG-software/.env` repo root,
///      canonicalized. `CARGO_MANIFEST_DIR` is baked in at compile time and
///      points at `desktop/src-tauri`, so this is stable regardless of the
///      runtime working directory.
///
/// We never read or log the file's *contents* — only its path is handled here.
fn resolve_env_file() -> Option<String> {
    if let Ok(explicit) = std::env::var("TCG_ENV_FILE") {
        if !explicit.trim().is_empty() {
            return Some(explicit);
        }
    }
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let candidate = std::path::Path::new(manifest_dir)
        .join("..")
        .join("..")
        .join(".env");
    // Canonicalize so the sidecar gets an absolute path it can resolve from any
    // cwd. If the file is absent we still return the (lexical) path so the
    // failure is visible in logs rather than silently swallowed.
    match std::fs::canonicalize(&candidate) {
        Ok(abs) => Some(abs.to_string_lossy().into_owned()),
        Err(_) => Some(candidate.to_string_lossy().into_owned()),
    }
}

/// Spawn the bundled backend sidecar, wiring args + the CORS/.env environment,
/// and stash the child handle in managed state. Streams the sidecar's
/// stdout/stderr to this process's log so backend errors are visible, and
/// polls `/health` in the background, logging once the API is ready.
fn spawn_sidecar(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let port = SIDECAR_PORT.to_string();

    let mut command = app
        .shell()
        // `sidecar` takes the *filename* from `externalBin` (no target-triple
        // suffix, no path) — Tauri resolves the actual binary at runtime.
        .sidecar("tcg-backend")?
        .args([
            "--host",
            SIDECAR_HOST,
            "--port",
            &port,
            "--log-level",
            "warning",
        ])
        // Critical: let the packaged webview origin through the backend's CORS.
        .env("TCG_CORS_ORIGINS", SIDECAR_CORS_ORIGINS);

    // Point the sidecar at the repo `.env` regardless of its working directory
    // so DWH_*/APP_DB_* creds resolve. (Contents never read here.)
    if let Some(env_file) = resolve_env_file() {
        eprintln!("[tcg-desktop] sidecar .env file: {env_file}");
        command = command.env("TCG_ENV_FILE", env_file);
    } else {
        eprintln!("[tcg-desktop] WARNING: could not resolve a .env path for the sidecar");
    }

    let (mut rx, child) = command.spawn()?;
    eprintln!(
        "[tcg-desktop] spawned backend sidecar (pid {}) on http://{}:{}",
        child.pid(),
        SIDECAR_HOST,
        SIDECAR_PORT
    );

    // Persist the child handle for shutdown.
    app.state::<SidecarProcess>()
        .0
        .lock()
        .expect("sidecar mutex poisoned")
        .replace(child);

    // Forward sidecar output to our stderr for diagnostics.
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    eprint!("[sidecar] {}", String::from_utf8_lossy(&bytes));
                }
                CommandEvent::Stderr(bytes) => {
                    eprint!("[sidecar] {}", String::from_utf8_lossy(&bytes));
                }
                CommandEvent::Error(err) => {
                    eprintln!("[sidecar] process error: {err}");
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!("[sidecar] terminated: {payload:?}");
                }
                _ => {}
            }
        }
    });

    // Best-effort readiness probe so the log shows when /health is up. The
    // webview can load immediately; the frontend's own retry/SWR handles the
    // brief window before the backend answers. Runs on a plain OS thread (not
    // the async runtime) since it only blocks on a short TCP connect loop.
    std::thread::spawn(move || {
        let health_url = format!("http://{SIDECAR_HOST}:{SIDECAR_PORT}/health");
        for attempt in 1..=60u32 {
            std::thread::sleep(std::time::Duration::from_millis(500));
            if std::net::TcpStream::connect((SIDECAR_HOST, SIDECAR_PORT)).is_ok() {
                eprintln!(
                    "[tcg-desktop] backend port open after ~{} ms ({})",
                    attempt * 500,
                    health_url
                );
                return;
            }
        }
        eprintln!("[tcg-desktop] WARNING: backend port not open after 30s ({health_url})");
    });

    Ok(())
}

/// Kill the sidecar if it is still running. Idempotent.
fn kill_sidecar(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<SidecarProcess>() {
        if let Some(child) = state.0.lock().expect("sidecar mutex poisoned").take() {
            let pid = child.pid();
            match child.kill() {
                Ok(()) => eprintln!("[tcg-desktop] killed backend sidecar (pid {pid})"),
                Err(err) => eprintln!("[tcg-desktop] failed to kill sidecar (pid {pid}): {err}"),
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(SidecarProcess(Mutex::new(None)))
        .setup(|app| {
            if let Err(err) = spawn_sidecar(&app.handle().clone()) {
                // A backend that never starts makes the app useless, but we let
                // the window open so the failure is visible to the user/log
                // rather than crashing silently at startup.
                eprintln!("[tcg-desktop] FAILED to spawn backend sidecar: {err}");
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| match event {
            // Fires when the last window is closed / quit is requested, and on
            // process exit. Either way, ensure the backend is terminated.
            RunEvent::ExitRequested { .. } => kill_sidecar(app_handle),
            RunEvent::Exit => kill_sidecar(app_handle),
            _ => {}
        });
}
