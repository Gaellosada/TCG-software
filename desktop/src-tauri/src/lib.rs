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
use tauri_plugin_shell::process::{Command, CommandChild, CommandEvent};
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

/// Bounded retry for a TRANSIENT sidecar spawn failure. On a managed Windows
/// box an antivirus real-time scan can momentarily lock the freshly-staged
/// one-file sidecar exe (`ERROR_SHARING_VIOLATION` = os error 32), which clears
/// within a few hundred ms. A small number of attempts with a short backoff
/// self-heals that without meaningfully delaying a DETERMINISTIC failure (missing
/// exe / policy block), which is not retried at all — see `is_transient_spawn_error`.
const SPAWN_MAX_ATTEMPTS: u32 = 3;
const SPAWN_RETRY_BACKOFF: Duration = Duration::from_millis(200);

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

/// Is a sidecar `spawn()` failure worth retrying? Only TRANSIENT conditions —
/// an antivirus real-time scan briefly locking the freshly-staged one-file exe —
/// clear on their own within a few hundred ms. That surfaces on Windows as
/// `ERROR_SHARING_VIOLATION` (raw os error 32), or cross-platform as
/// `ErrorKind::WouldBlock` (the OS reporting the resource temporarily
/// unavailable). DETERMINISTIC failures do NOT change on retry — retrying only
/// delays the inevitable error — so they return false and fail fast: os error 2
/// (FILE_NOT_FOUND, exe quarantined/never staged), 5 (ACCESS_DENIED — an
/// AppLocker/SRP/AV execute-block or ACL is a policy verdict, not a lock; a mere
/// scan lock shows as 32, handled above), 193 (BAD_EXE_FORMAT), 225/226
/// (Defender VIRUS_INFECTED/VIRUS_DELETED). Pure so it is unit-testable without
/// spawning.
fn is_transient_spawn_error(kind: std::io::ErrorKind, raw_os_error: Option<i32>) -> bool {
    // Windows ERROR_SHARING_VIOLATION: the exe is momentarily locked (AV scan /
    // still being flushed by the one-file bootloader). The canonical transient.
    // The check is intentionally NOT `cfg!(windows)`-gated so this fn (and its
    // unit test) stay platform-agnostic: on Linux raw 32 is EPIPE, which a
    // `spawn()` cannot realistically return, so at worst this costs a bounded
    // (<=400ms) retry there — never a correctness problem.
    if raw_os_error == Some(32) {
        return true;
    }
    // "Resource temporarily unavailable" (EAGAIN/EWOULDBLOCK on Unix; some
    // Windows locks map here too). Deliberately NOT retried: os error 5, which
    // is a deterministic policy/ACL denial rather than a transient lock.
    matches!(kind, std::io::ErrorKind::WouldBlock)
}

/// Facts probed about the sidecar's launch environment, kept separate from the
/// string formatting so the formatter (`format_sidecar_diagnostics`) is
/// unit-testable without touching the filesystem.
struct SidecarProbe {
    /// Resolved absolute sidecar path (display form), or the reason it could not
    /// be resolved.
    path: Result<String, String>,
    /// Sidecar file metadata: `Ok(size_bytes)` if it exists, `Err(reason)` if
    /// missing/unreadable. Only consulted when `path` is `Ok`.
    file: Result<u64, String>,
    /// `%TEMP%`/`$TMPDIR` (display form) — where the one-file bootloader unpacks
    /// `_MEIxxxx`.
    temp_dir: String,
    /// Whether a probe file could be written into `temp_dir`.
    temp_writable: Result<(), String>,
}

/// Fold a `SidecarProbe` into the one-line diagnostic appended to a spawn-failure
/// message: resolved path + exists/size + temp writability. Pure.
fn format_sidecar_diagnostics(probe: &SidecarProbe) -> String {
    let mut out = String::new();
    match &probe.path {
        Ok(path) => {
            out.push_str(&format!("resolved sidecar path={path}"));
            match &probe.file {
                Ok(size) => out.push_str(&format!(" exists=yes size={size}B")),
                Err(reason) => out.push_str(&format!(" exists=NO ({reason})")),
            }
        }
        Err(reason) => out.push_str(&format!("could not resolve sidecar path: {reason}")),
    }
    match &probe.temp_writable {
        Ok(()) => out.push_str(&format!("; temp_dir={} writable=yes", probe.temp_dir)),
        Err(reason) => out.push_str(&format!("; temp_dir={} writable=NO ({reason})", probe.temp_dir)),
    }
    out
}

/// Probe the filesystem to reproduce the shell plugin's sidecar path resolution
/// (`<exe-dir>/tcg-backend[.exe]`, mirroring
/// `tauri_plugin_shell::process::relative_command_path`) plus `%TEMP%`
/// writability. Because `.sidecar()` only *joins* this path (no existence
/// check), a missing/quarantined exe fails at `spawn()` — which is exactly what
/// this annotates. Does the I/O; the formatting is delegated to the pure
/// `format_sidecar_diagnostics`.
fn probe_sidecar() -> SidecarProbe {
    let (path, file) = match std::env::current_exe() {
        Ok(exe) => match exe.parent() {
            Some(dir) => {
                let name = if cfg!(windows) { "tcg-backend.exe" } else { "tcg-backend" };
                let candidate = dir.join(name);
                let file = match std::fs::metadata(&candidate) {
                    Ok(m) => Ok(m.len()),
                    Err(e) => Err(e.to_string()),
                };
                (Ok(candidate.display().to_string()), file)
            }
            None => (Err("current_exe has no parent dir".into()), Ok(0)),
        },
        Err(e) => (Err(format!("current_exe() failed: {e}")), Ok(0)),
    };
    let temp_dir = std::env::temp_dir();
    let probe_file = temp_dir.join(format!(".tcg_write_probe_{}", std::process::id()));
    let temp_writable = match std::fs::write(&probe_file, b"x") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe_file);
            Ok(())
        }
        Err(e) => Err(e.to_string()),
    };
    SidecarProbe {
        path,
        file,
        temp_dir: temp_dir.display().to_string(),
        temp_writable,
    }
}

/// One-line spawn-failure diagnostic: absolute sidecar path + exists/size + temp
/// writability. Thin wrapper composing the FS probe with the pure formatter.
fn sidecar_diagnostics() -> String {
    format_sidecar_diagnostics(&probe_sidecar())
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

    // Resolve the .env path (Settings-written config dir, next to the exe, an
    // explicit override, or the dev fallback) ONCE and log it once — a spawn
    // retry below must not re-run or re-log this. The file's contents are never
    // read here.
    let env_file = resolve_env_file(app);
    match &env_file {
        Some(f) => eprintln!("[tcg-desktop] sidecar .env file: {f}"),
        None => eprintln!(
            "[tcg-desktop] WARNING: no .env found — set database credentials in \
             Settings (writes {}). The backend will not start until then.",
            app_config_env_path(app)
                .unwrap_or_else(|_| "<app config dir>/.env".into())
                .display()
        ),
    }

    // Build a FRESH sidecar command for each spawn attempt: `Command` is not
    // `Clone` and `spawn(self)` consumes it, so the retry loop below has to
    // reconstruct it. This is cheap (no I/O) and does NOT log, so retries stay
    // quiet and the happy path is unchanged.
    let build_command = || -> Result<Command, Box<dyn std::error::Error>> {
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
        if let Some(f) = &env_file {
            command = command.env("TCG_ENV_FILE", f);
        }
        Ok(command)
    };

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

    // Spawn with a bounded retry on TRANSIENT failures only (e.g. an antivirus
    // real-time scan momentarily locking the freshly-staged sidecar exe =
    // ERROR_SHARING_VIOLATION 32). Deterministic failures (missing exe = 2,
    // policy/ACL block = 5, bad format = 193) fall through on the first attempt
    // with a rich diagnostic. The happy path spawns on the first try — no added
    // latency, no changed ordering.
    let mut attempt = 1u32;
    let (mut rx, child) = loop {
        // A build failure (sidecar not configured under `externalBin`) is a
        // deterministic config error, never transient — surface it immediately.
        let command = build_command()?;
        match command.spawn() {
            Ok(pair) => break pair,
            Err(e) => {
                let (kind, os_n) = match &e {
                    tauri_plugin_shell::Error::Io(io) => (Some(io.kind()), io.raw_os_error()),
                    _ => (None, None),
                };
                if attempt < SPAWN_MAX_ATTEMPTS
                    && kind.is_some_and(|k| is_transient_spawn_error(k, os_n))
                {
                    eprintln!(
                        "[tcg-desktop] sidecar spawn attempt {attempt}/{SPAWN_MAX_ATTEMPTS} hit a \
                         transient error (os error {}); retrying in {} ms",
                        os_n.map(|n| n.to_string()).unwrap_or_else(|| "none".into()),
                        SPAWN_RETRY_BACKOFF.as_millis()
                    );
                    attempt += 1;
                    std::thread::sleep(SPAWN_RETRY_BACKOFF);
                    continue;
                }
                // Surface the os-error NUMBER + resolved path/existence + temp
                // writability instead of the opaque "(os error N)". Windows: 2 =
                // FILE_NOT_FOUND (exe quarantined/deleted/never staged), 5 =
                // ACCESS_DENIED (AppLocker/SRP/AV execute-block or ACL), 32 =
                // SHARING_VIOLATION (AV scan lock), 193 = BAD_EXE_FORMAT
                // (corrupt), 225/226 = VIRUS_INFECTED/VIRUS_DELETED (Defender),
                // 740 = ELEVATION_REQUIRED. NB: a temp-EXTRACTION block does NOT
                // reach here — spawn() returns Ok and the child then TERMINATES.
                let os_str = os_n.map(|n| n.to_string()).unwrap_or_else(|| "none".into());
                let tried = if attempt > 1 {
                    format!(" after {attempt} attempts")
                } else {
                    String::new()
                };
                let msg = format!(
                    "spawn failed{tried} (os error {os_str}): {e} | {}",
                    sidecar_diagnostics()
                );
                eprintln!("[tcg-desktop] {msg}");
                return Err(msg.into());
            }
        }
    };
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

#[cfg(test)]
mod tests {
    use super::{format_sidecar_diagnostics, is_transient_spawn_error, SidecarProbe};
    use std::io::ErrorKind;

    #[test]
    fn transient_only_for_sharing_violation_and_would_block() {
        // Windows ERROR_SHARING_VIOLATION (AV scan lock) -> retry, whatever the
        // mapped ErrorKind is.
        assert!(is_transient_spawn_error(ErrorKind::Other, Some(32)));
        // "Resource temporarily unavailable" (EAGAIN/EWOULDBLOCK) -> retry.
        assert!(is_transient_spawn_error(ErrorKind::WouldBlock, None));
        assert!(is_transient_spawn_error(ErrorKind::WouldBlock, Some(11)));

        // Deterministic verdicts must NOT be retried (retrying only delays them).
        assert!(!is_transient_spawn_error(ErrorKind::NotFound, Some(2))); // FILE_NOT_FOUND
        // ACCESS_DENIED (5) is a policy/ACL block, not a transient lock — a mere
        // scan lock surfaces as 32 (asserted above). This is the key boundary.
        assert!(!is_transient_spawn_error(ErrorKind::PermissionDenied, Some(5)));
        assert!(!is_transient_spawn_error(ErrorKind::Other, Some(193))); // BAD_EXE_FORMAT
        assert!(!is_transient_spawn_error(ErrorKind::Other, Some(225))); // VIRUS_INFECTED
        assert!(!is_transient_spawn_error(ErrorKind::Other, Some(226))); // VIRUS_DELETED
        // No os error + a generic kind -> no retry.
        assert!(!is_transient_spawn_error(ErrorKind::Other, None));
    }

    #[test]
    fn diagnostics_reports_existing_file_and_writable_temp() {
        let s = format_sidecar_diagnostics(&SidecarProbe {
            path: Ok("/opt/app/tcg-backend".into()),
            file: Ok(12_345),
            temp_dir: "/tmp".into(),
            temp_writable: Ok(()),
        });
        assert!(s.contains("resolved sidecar path=/opt/app/tcg-backend"), "{s}");
        assert!(s.contains("exists=yes size=12345B"), "{s}");
        assert!(s.contains("temp_dir=/tmp writable=yes"), "{s}");
    }

    #[test]
    fn diagnostics_reports_missing_file_and_unwritable_temp() {
        let s = format_sidecar_diagnostics(&SidecarProbe {
            path: Ok("C:\\Program Files\\TCG\\tcg-backend.exe".into()),
            file: Err("The system cannot find the file specified. (os error 2)".into()),
            temp_dir: "C:\\Temp".into(),
            temp_writable: Err("Access is denied. (os error 5)".into()),
        });
        assert!(
            s.contains("exists=NO (The system cannot find the file specified."),
            "{s}"
        );
        assert!(s.contains("writable=NO (Access is denied."), "{s}");
    }

    #[test]
    fn diagnostics_reports_unresolvable_path_and_ignores_file() {
        let s = format_sidecar_diagnostics(&SidecarProbe {
            path: Err("current_exe() failed: nope".into()),
            file: Ok(0),
            temp_dir: "/tmp".into(),
            temp_writable: Ok(()),
        });
        assert!(
            s.contains("could not resolve sidecar path: current_exe() failed"),
            "{s}"
        );
        // `file` is not consulted when the path itself could not be resolved.
        assert!(!s.contains("exists="), "{s}");
    }
}
