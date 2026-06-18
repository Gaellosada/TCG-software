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

/// Resolve the `.env` the sidecar should read, searching (first hit wins):
///   1. `TCG_ENV_FILE` env var — explicit override (escape hatch).
///   2. `<exe-dir>/.env` — the `.env` sitting in the SAME FOLDER as the running
///      executable (the install dir for the packaged app). This is the place to
///      drop credentials for the desktop build.
///   3. Dev fallback: `<crate-dir>/../../.env` (the repo root), so `tauri dev`
///      / running from a checkout keeps working.
///
/// Only the PATH is handled here — the file's contents are never read or logged.
/// Returns the first EXISTING candidate, or None (the sidecar then fails fast
/// with a clear "DWH_* not set" error; the caller logs where to drop the file).
fn resolve_env_file() -> Option<String> {
    fn abs(p: std::path::PathBuf) -> Option<String> {
        Some(
            std::fs::canonicalize(&p)
                .unwrap_or(p)
                .to_string_lossy()
                .into_owned(),
        )
    }

    if let Ok(explicit) = std::env::var("TCG_ENV_FILE") {
        if !explicit.trim().is_empty() {
            return Some(explicit);
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let cand = dir.join(".env");
            if cand.is_file() {
                return abs(cand);
            }
        }
    }

    let dev = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join(".env");
    if dev.is_file() {
        return abs(dev);
    }

    None
}

/// The directory of the running executable (the install dir for a packaged
/// build) — i.e. where the user should drop their `.env`. Used only for a
/// helpful log line when no `.env` is found.
fn exe_dir_hint() -> String {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join(".env").to_string_lossy().into_owned()))
        .unwrap_or_else(|| "<folder of the app .exe>/.env".into())
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

    // Give the sidecar the resolved .env path (next to the exe, or an explicit
    // override / dev fallback). The file's contents are never read here.
    if let Some(env_file) = resolve_env_file() {
        eprintln!("[tcg-desktop] sidecar .env file: {env_file}");
        command = command.env("TCG_ENV_FILE", env_file);
    } else {
        eprintln!(
            "[tcg-desktop] WARNING: no .env found — drop one at {} (next to the app \
             executable). The backend will not start until then.",
            exe_dir_hint()
        );
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
