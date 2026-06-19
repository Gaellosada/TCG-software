//! Tauri v2 desktop wrapper for Trajectoire CAP.
//!
//! This crate is intentionally ADDITIVE: it spawns the existing FastAPI backend
//! as a bundled PyInstaller *sidecar* (`binaries/tcg-backend-<target-triple>`)
//! and shows the existing Vite/React frontend in a single window. It forks
//! neither the backend (`tcg/`) nor the frontend (`frontend/`) nor `start.sh`.
//!
//! Lifecycle: the sidecar is spawned in `setup`, its child handle is kept in
//! managed state, and it is killed when the app exits so no orphaned backend
//! process is left behind. The sidecar is also *restartable* (kill + respawn)
//! so changing DB credentials from the in-app Settings can take effect without
//! relaunching the whole app.
//!
//! Credentials: instead of asking the user to hand-edit a `.env`, the Settings
//! page writes one to `<app_config_dir>/.env` (Linux: `~/.config/<id>/.env`)
//! via the `save_db_credentials` command, then asks for a backend restart so
//! the freshly-spawned sidecar reads the new values.

use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, RunEvent};
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

// ---------------------------------------------------------------------------
// Database credentials (in-app Settings <-> `<app_config_dir>/.env`)
// ---------------------------------------------------------------------------

/// The DB connection fields the Settings UI edits. Mirrors the backend's env
/// vars but with friendlier field names. `host`/`port`/`db`/`sslmode` are
/// shared by both pools; `dwh_*` is the read-only market role, `app_db_*` is
/// the read-write app-data role. APP_DB host/port/db are intentionally NOT
/// stored: the app-data pool inherits them from DWH_* (see
/// `tcg/persistence/_pg.py::load_app_db_config`).
///
/// Serialized to/from JS as the matching camel/snake field names; serde keeps
/// the snake_case used here, which the frontend mirrors verbatim.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct DbCredentials {
    host: String,
    port: String,
    db: String,
    sslmode: String,
    dwh_user: String,
    dwh_password: String,
    app_db_user: String,
    app_db_password: String,
}

impl Default for DbCredentials {
    /// Sensible defaults when no `.env` exists yet — matches the backend's own
    /// fallbacks (`DWH_PORT` -> 5432, `DWH_DB` -> "dwh", `DWH_SSLMODE` ->
    /// "require"); secret/user fields start empty so the form prompts for them.
    fn default() -> Self {
        Self {
            host: String::new(),
            port: "5432".into(),
            db: "dwh".into(),
            sslmode: "require".into(),
            dwh_user: String::new(),
            dwh_password: String::new(),
            app_db_user: String::new(),
            app_db_password: String::new(),
        }
    }
}

/// The `.env` path the Settings page writes and `resolve_env_file` prefers:
/// `<app_config_dir>/.env`. `app_config_dir()` resolves to
/// `config_dir()/<bundle identifier>` (Linux: `~/.config/com.trajectoirecap.tcg`).
fn app_config_env_path(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    app.path()
        .app_config_dir()
        .map(|dir| dir.join(".env"))
        .map_err(|e| format!("could not resolve app config dir: {e}"))
}

/// Resolve the `.env` the sidecar should read, searching (first hit wins):
///   1. `TCG_ENV_FILE` env var — explicit override (escape hatch).
///   2. `<app_config_dir>/.env` — where the in-app Settings page writes
///      credentials (Linux: `~/.config/com.trajectoirecap.tcg/.env`). This is
///      the normal location for the packaged desktop build.
///   3. `<exe-dir>/.env` — a `.env` next to the running executable (manual
///      install fallback).
///   4. Dev fallback: `<crate-dir>/../../.env` (the repo root), so `tauri dev`
///      / running from a checkout keeps working.
///
/// Only the PATH is handled here — the file's contents are never read or logged.
/// Returns the first EXISTING candidate, or None (the sidecar then fails fast
/// with a clear "DWH_* not set" error; the frontend's banner tells the user to
/// set credentials in Settings).
fn resolve_env_file(app: &AppHandle) -> Option<String> {
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

    // Where Settings writes — checked before exe-dir so saved creds win.
    if let Ok(cfg) = app_config_env_path(app) {
        if cfg.is_file() {
            return abs(cfg);
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

/// Spawn the bundled backend sidecar, wiring args + the CORS/.env environment,
/// and stash the child handle in managed state. Any previously-spawned child is
/// killed first so this doubles as the restart primitive. Streams the sidecar's
/// stdout/stderr to this process's log so backend errors are visible, and polls
/// the port in the background, logging once it is open.
fn spawn_sidecar(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    // Restartable: terminate any existing child before launching a new one so
    // we never leave an orphan holding port 8000.
    kill_sidecar(app);

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

    // Give the sidecar the resolved .env path (Settings-written config dir,
    // next to the exe, an explicit override, or the dev fallback). The file's
    // contents are never read here.
    if let Some(env_file) = resolve_env_file(app) {
        eprintln!("[tcg-desktop] sidecar .env file: {env_file}");
        command = command.env("TCG_ENV_FILE", env_file);
    } else {
        eprintln!(
            "[tcg-desktop] WARNING: no .env found — set database credentials in \
             Settings (writes {}). The backend will not start until then.",
            app_config_env_path(app).unwrap_or_else(|_| "<app config dir>/.env".into()).display()
        );
    }

    let (mut rx, child) = command.spawn()?;
    eprintln!(
        "[tcg-desktop] spawned backend sidecar (pid {}) on http://{}:{}",
        child.pid(),
        SIDECAR_HOST,
        SIDECAR_PORT
    );

    // Persist the child handle for shutdown / next restart.
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

    // Best-effort readiness probe so the log shows when the port is up. The
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

/// Kill the sidecar if it is still running. Idempotent — safe to call before a
/// respawn and on exit.
fn kill_sidecar(app: &AppHandle) {
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

// ---------------------------------------------------------------------------
// `.env` (de)serialization for credentials. KEY=VALUE, one per line.
// ---------------------------------------------------------------------------

/// Parse the keys we care about out of `.env` text into a map. Lines that are
/// blank, comments (`#`), or have no `=` are ignored; a surrounding pair of
/// matching single/double quotes around the value is stripped. We do NOT do
/// shell-style expansion — values are taken literally (matches how psycopg /
/// `dotenv_values` treat them for our keys, which are plain connection params).
fn parse_env_pairs(text: &str) -> std::collections::HashMap<String, String> {
    let mut map = std::collections::HashMap::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // Optional `export ` prefix tolerance.
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((key, val)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim().to_string();
        let mut val = val.trim();
        if val.len() >= 2 {
            let b = val.as_bytes();
            if (b[0] == b'"' && b[b.len() - 1] == b'"')
                || (b[0] == b'\'' && b[b.len() - 1] == b'\'')
            {
                val = &val[1..val.len() - 1];
            }
        }
        map.insert(key, val.to_string());
    }
    map
}

/// Render the credentials as `.env` text — exactly the eight keys, one per
/// line, in a stable order. APP_DB host/port/db are intentionally omitted (the
/// app-data pool inherits them from DWH_*). Values are written verbatim; we do
/// not quote, so a value must not contain a newline (UI fields are single-line).
fn credentials_to_env(creds: &DbCredentials) -> String {
    format!(
        "DWH_HOST={}\n\
         DWH_PORT={}\n\
         DWH_DB={}\n\
         DWH_SSLMODE={}\n\
         DWH_USER={}\n\
         DWH_PASSWORD={}\n\
         APP_DB_USER={}\n\
         APP_DB_PASSWORD={}\n",
        creds.host,
        creds.port,
        creds.db,
        creds.sslmode,
        creds.dwh_user,
        creds.dwh_password,
        creds.app_db_user,
        creds.app_db_password,
    )
}

// ---------------------------------------------------------------------------
// Tauri commands invoked from the Settings page.
// ---------------------------------------------------------------------------

/// Read the DB credentials currently stored at `<app_config_dir>/.env`. Returns
/// defaults if the file is absent. Never logs values.
#[tauri::command]
fn get_db_credentials(app: AppHandle) -> DbCredentials {
    let mut creds = DbCredentials::default();
    let Ok(path) = app_config_env_path(&app) else {
        return creds;
    };
    let Ok(text) = std::fs::read_to_string(&path) else {
        // Missing/unreadable -> defaults (first run).
        return creds;
    };
    let pairs = parse_env_pairs(&text);
    if let Some(v) = pairs.get("DWH_HOST") {
        creds.host = v.clone();
    }
    if let Some(v) = pairs.get("DWH_PORT") {
        creds.port = v.clone();
    }
    if let Some(v) = pairs.get("DWH_DB") {
        creds.db = v.clone();
    }
    if let Some(v) = pairs.get("DWH_SSLMODE") {
        creds.sslmode = v.clone();
    }
    if let Some(v) = pairs.get("DWH_USER") {
        creds.dwh_user = v.clone();
    }
    if let Some(v) = pairs.get("DWH_PASSWORD") {
        creds.dwh_password = v.clone();
    }
    if let Some(v) = pairs.get("APP_DB_USER") {
        creds.app_db_user = v.clone();
    }
    if let Some(v) = pairs.get("APP_DB_PASSWORD") {
        creds.app_db_password = v.clone();
    }
    creds
}

/// Write the DB credentials to `<app_config_dir>/.env` (creating the dir) and
/// restart the sidecar so it picks up the new values. Returns Err(message) on
/// any failure. Never logs credential VALUES — only the file path.
#[tauri::command]
fn save_db_credentials(app: AppHandle, creds: DbCredentials) -> Result<(), String> {
    let path = app_config_env_path(&app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("could not create config dir {}: {e}", parent.display()))?;
    }
    std::fs::write(&path, credentials_to_env(&creds))
        .map_err(|e| format!("could not write {}: {e}", path.display()))?;
    eprintln!("[tcg-desktop] saved DB credentials to {}", path.display());

    // Restart so the new creds take effect (kill old child + spawn fresh).
    spawn_sidecar(&app).map_err(|e| format!("saved credentials, but backend restart failed: {e}"))
}

/// Restart the backend sidecar (kill + spawn). Exposed so the UI can force a
/// reconnect without changing credentials.
#[tauri::command]
fn restart_backend(app: AppHandle) -> Result<(), String> {
    spawn_sidecar(&app).map_err(|e| format!("backend restart failed: {e}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(SidecarProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            get_db_credentials,
            save_db_credentials,
            restart_backend
        ])
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
