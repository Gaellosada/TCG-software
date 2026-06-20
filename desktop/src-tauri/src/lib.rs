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

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Local address the sidecar binds to. Must match the args granted in
/// `capabilities/default.json` and the frontend's API base URL.
const SIDECAR_HOST: &str = "127.0.0.1";
const SIDECAR_PORT: u16 = 8000;

/// CORS origins the bundled webview is served from. The packaged document origin
/// differs by platform: Linux/macOS use the custom `tauri://localhost` scheme,
/// while Windows' WebView2 serves over HTTP — `http://tauri.localhost` (Tauri v2
/// moved the Windows origin from `https://` to `http://`, see the v2 migration
/// docs). We list ALL of them (plus `https://tauri.localhost` for safety and the
/// Vite dev origin so the same value works under `tauri dev`). Omitting the
/// Windows `http://tauri.localhost` here is what made the packaged Windows build
/// fail every fetch with "Failed to fetch" / "Backend unreachable". The backend
/// otherwise defaults to allowing only the Vite dev origin.
const SIDECAR_CORS_ORIGINS: &str =
    "tauri://localhost,https://tauri.localhost,http://tauri.localhost,http://localhost:5173";

/// Holds the spawned sidecar's child handle so we can terminate it on exit.
/// `CommandChild::kill` consumes the handle, hence `Option` + `take()`.
struct SidecarProcess(Mutex<Option<CommandChild>>);

/// Serializes the whole kill→spawn restart sequence. `SidecarProcess` only
/// guards the handle *slot*; this guards the operation, so two concurrent
/// commands (a double-clicked Save, or Save racing a manual restart) can't both
/// spawn a sidecar and leak/orphan one. Held for the duration of `spawn_sidecar`.
struct RestartLock(Mutex<()>);

/// Block until nothing is accepting connections on `port`, or `timeout` elapses
/// (returns whether the port became free). Used after killing the old sidecar
/// so a fast restart doesn't try to bind 8000 before the previous process has
/// released it. Returns `true` as soon as a connect attempt is refused.
fn wait_for_port_free(host: &str, port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if std::net::TcpStream::connect((host, port)).is_err() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// One-shot `GET /health`, true only on an HTTP `200`. A real health check (not
/// a bare TCP connect, which would also succeed against a stale process holding
/// the port) without pulling in an HTTP client crate.
fn health_ok(host: &str, port: u16) -> bool {
    use std::io::{Read, Write};
    let Ok(mut stream) = std::net::TcpStream::connect((host, port)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(800)));
    let req =
        format!("GET /health HTTP/1.0\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 256];
    match stream.read(&mut buf) {
        Ok(n) if n > 0 => {
            let head = String::from_utf8_lossy(&buf[..n]);
            head.starts_with("HTTP/1.") && head.contains(" 200 ")
        }
        _ => false,
    }
}

/// Write a secrets-bearing file owner-only (0600 on Unix), tightening perms even
/// if it pre-existed with a looser mode.
fn write_private(path: &std::path::Path, contents: &str) -> std::io::Result<()> {
    std::fs::write(path, contents)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

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

/// A dependency-free session marker for the log header: seconds since the Unix
/// epoch. We don't pull in `chrono` just to stamp a delimiter — the epoch second
/// is enough to tell sessions apart and to correlate with other timestamps.
fn chrono_like_timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Path to the backend sidecar's log file: `<app_log_dir>/backend.log`. The
/// sidecar runs with `console=False`, so its stderr (dwh connection errors,
/// tracebacks) is otherwise invisible once the window hides the console — we tee
/// it here so the user can open this file when a connection fails. `app_log_dir`
/// resolves to (Linux/Windows) `<local_data_dir>/<bundle id>/logs`, (macOS)
/// `~/Library/Logs/<bundle id>`. Falls back to `<app_config_dir>` (where the
/// `.env` lives) if the log dir can't be resolved, so we always have a path.
fn backend_log_path(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let dir = app
        .path()
        .app_log_dir()
        .or_else(|_| app.path().app_config_dir())
        .map_err(|e| format!("could not resolve a log dir: {e}"))?;
    Ok(dir.join("backend.log"))
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
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            if std::path::Path::new(trimmed).is_file() {
                return Some(explicit);
            }
            eprintln!(
                "[tcg-desktop] WARNING: TCG_ENV_FILE='{explicit}' is not a file; \
                 ignoring it and searching the default locations"
            );
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
    // Serialize the whole kill→spawn so concurrent restart commands can't
    // double-spawn. Bound to a named State so the guard outlives the statement;
    // held until this function returns.
    let restart_lock = app.state::<RestartLock>();
    let _restart_guard = restart_lock
        .0
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());

    // Restartable: terminate any existing child before launching a new one so
    // we never leave an orphan holding port 8000.
    kill_sidecar(app);

    // Wait for the old sidecar to release the port before binding it. The
    // sidecar's parent-death watchdog exits it promptly once we kill the
    // bootloader; without this, a fast restart could hit "address in use".
    if !wait_for_port_free(SIDECAR_HOST, SIDECAR_PORT, Duration::from_secs(5)) {
        eprintln!(
            "[tcg-desktop] WARNING: port {SIDECAR_PORT} still in use 5s after \
             kill; spawning anyway (a stale process may be holding it)"
        );
    }

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

    // Open the backend log file in APPEND mode and write a session header. We
    // append (rather than truncate) on purpose: `spawn_sidecar` is also the
    // *restart* primitive, and truncating on every restart would erase the very
    // error the user is trying to read after a failed reconnect. The header
    // delimits each spawn so the latest session is easy to find. The path is
    // logged so it shows up in the (dev) console too. Best-effort: if the file
    // can't be opened we still forward to stderr and the app keeps working.
    let log_file = match backend_log_path(app) {
        Ok(path) => {
            if let Some(parent) = path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            eprintln!("[tcg-desktop] backend log file: {}", path.display());
            match std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
            {
                Ok(mut f) => {
                    use std::io::Write as _;
                    // backend.log can surface dwh errors; keep it owner-only.
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        let _ = std::fs::set_permissions(
                            &path,
                            std::fs::Permissions::from_mode(0o600),
                        );
                    }
                    let _ = writeln!(
                        f,
                        "\n===== tcg-backend session {} =====",
                        chrono_like_timestamp()
                    );
                    Some(f)
                }
                Err(e) => {
                    eprintln!("[tcg-desktop] could not open backend log {}: {e}", path.display());
                    None
                }
            }
        }
        Err(e) => {
            eprintln!("[tcg-desktop] no backend log path: {e}");
            None
        }
    };

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
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .replace(child);

    // Forward sidecar output to our stderr for diagnostics AND tee it to the log
    // file (the sidecar never logs credential VALUES — it only echoes keys/paths
    // — so teeing its output is safe). `log_file` is moved into the task and the
    // writes are best-effort (`let _ =`): a logging failure must never take down
    // the backend stream.
    // Shared flag the output task flips if the sidecar dies, so the readiness
    // probe can stop waiting instead of burning the full 30s.
    let died = Arc::new(AtomicBool::new(false));
    let died_tee = died.clone();

    tauri::async_runtime::spawn(async move {
        let mut log_file = log_file;
        let mut tee = |line: &str| {
            if let Some(f) = log_file.as_mut() {
                use std::io::Write as _;
                let _ = f.write_all(line.as_bytes());
            }
        };
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let s = String::from_utf8_lossy(&bytes);
                    eprint!("[sidecar] {s}");
                    tee(&s);
                }
                CommandEvent::Stderr(bytes) => {
                    let s = String::from_utf8_lossy(&bytes);
                    eprint!("[sidecar] {s}");
                    tee(&s);
                }
                CommandEvent::Error(err) => {
                    let line = format!("[sidecar] process error: {err}\n");
                    eprint!("{line}");
                    tee(&line);
                    died_tee.store(true, Ordering::SeqCst);
                }
                CommandEvent::Terminated(payload) => {
                    let line = format!("[sidecar] terminated: {payload:?}\n");
                    eprint!("{line}");
                    tee(&line);
                    died_tee.store(true, Ordering::SeqCst);
                }
                _ => {}
            }
        }
    });

    // Best-effort readiness probe so the log shows when the backend is healthy.
    // The webview can load immediately; the frontend's own retry/SWR handles the
    // brief window before the backend answers. Runs on a plain OS thread (not
    // the async runtime) since it only blocks on a short poll loop. It issues a
    // real GET /health (not a bare TCP connect, which would also "succeed"
    // against a stale holder of the port) and short-circuits if the sidecar dies.
    std::thread::spawn(move || {
        let health_url = format!("http://{SIDECAR_HOST}:{SIDECAR_PORT}/health");
        for attempt in 1..=60u32 {
            std::thread::sleep(Duration::from_millis(500));
            if died.load(Ordering::SeqCst) {
                eprintln!(
                    "[tcg-desktop] backend sidecar exited before becoming healthy \
                     — see backend.log ({health_url})"
                );
                return;
            }
            if health_ok(SIDECAR_HOST, SIDECAR_PORT) {
                eprintln!(
                    "[tcg-desktop] backend healthy after ~{} ms ({})",
                    attempt * 500,
                    health_url
                );
                return;
            }
        }
        eprintln!("[tcg-desktop] WARNING: backend not healthy after 30s ({health_url})");
    });

    Ok(())
}

/// Kill the sidecar if it is still running. Idempotent — safe to call before a
/// respawn and on exit.
fn kill_sidecar(app: &AppHandle) {
    if let Some(state) = app.try_state::<SidecarProcess>() {
        if let Some(child) =
            state.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).take()
        {
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
    // The webview is the trust boundary (not the React form): reject control
    // characters so a value can't inject or override another .env key.
    for (name, val) in [
        ("DWH_HOST", &creds.host),
        ("DWH_PORT", &creds.port),
        ("DWH_DB", &creds.db),
        ("DWH_SSLMODE", &creds.sslmode),
        ("DWH_USER", &creds.dwh_user),
        ("DWH_PASSWORD", &creds.dwh_password),
        ("APP_DB_USER", &creds.app_db_user),
        ("APP_DB_PASSWORD", &creds.app_db_password),
    ] {
        if val.contains('\n') || val.contains('\r') {
            return Err(format!("{name} must not contain a newline"));
        }
    }

    let path = app_config_env_path(&app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("could not create config dir {}: {e}", parent.display()))?;
        // The config dir holds plaintext DB credentials -> make it user-only.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700));
        }
    }
    // 0600 on Unix: plaintext creds must not be world-readable on a shared host.
    write_private(&path, &credentials_to_env(&creds))
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

/// Resolve the backend log-file path so the Settings page can show the user
/// where to look when a connection fails. Returns the path string (the file may
/// not exist yet if the sidecar has not written anything). Never reads the
/// file's contents.
#[tauri::command]
fn get_backend_log_path(app: AppHandle) -> Result<String, String> {
    backend_log_path(&app).map(|p| p.to_string_lossy().into_owned())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(SidecarProcess(Mutex::new(None)))
        .manage(RestartLock(Mutex::new(())))
        .invoke_handler(tauri::generate_handler![
            get_db_credentials,
            save_db_credentials,
            restart_backend,
            get_backend_log_path
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
