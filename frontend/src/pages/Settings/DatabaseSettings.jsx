import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import styles from './SettingsPage.module.css';

// Health endpoint of the auto-spawned sidecar. NOTE: /health is mounted at the
// ROOT of the backend, not under /api, so we hardcode the sidecar origin here
// (under Tauri the API base is http://127.0.0.1:8000/api — same host:port).
const HEALTH_URL = 'http://127.0.0.1:8000/health';

// The eight fields written to <app_config_dir>/.env by the Rust
// save_db_credentials command. Field names match the Rust DbCredentials struct
// (serde snake_case) verbatim — do not rename one side without the other.
const EMPTY_CREDS = {
  host: '',
  port: '5432',
  db: 'dwh',
  sslmode: 'require',
  dwh_user: '',
  dwh_password: '',
  app_db_user: '',
  app_db_password: '',
};

// Poll the backend /health until it returns 200 or we give up. Used after a
// save so the UI can report "Connected" once the freshly-restarted sidecar is
// answering. Returns true on success, false on timeout. Budget (80 * 500ms =
// 40s) deliberately exceeds the Rust-side readiness probe (~30s) plus margin
// for the one-file sidecar's first-run unpack, so a slow cold start doesn't
// falsely report failure before the backend is actually up.
async function waitForHealth({ attempts = 80, intervalMs = 500 } = {}) {
  for (let i = 0; i < attempts; i += 1) {
    try {
      const res = await fetch(HEALTH_URL, { cache: 'no-store' });
      if (res.ok) return true;
    } catch {
      // Backend still restarting / not listening yet — keep polling.
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return false;
}

// Desktop-only Settings section to edit the database connection. Rendered ONLY
// under Tauri (the parent SettingsPage gates it on isTauri()); web mode keeps
// using the server-side .env and never mounts this. On mount it prefills from
// the stored creds via the get_db_credentials command; on Save it writes them
// and polls health until the restarted sidecar is up.
function DatabaseSettings() {
  const [creds, setCreds] = useState(EMPTY_CREDS);
  const [status, setStatus] = useState({ kind: 'idle', message: '' });
  const [loaded, setLoaded] = useState(false);
  // Path to the sidecar's tee'd log file (backend.log). Shown so the user can
  // open it when a connection fails — the sidecar now runs with the console
  // hidden, so this file is where its dwh errors/tracebacks land.
  const [logPath, setLogPath] = useState('');

  useEffect(() => {
    let cancelled = false;
    invoke('get_db_credentials')
      .then((stored) => {
        if (cancelled) return;
        // Merge over defaults so a partial/empty file still yields a full form.
        setCreds({ ...EMPTY_CREDS, ...stored });
      })
      .catch((err) => {
        if (cancelled) return;
        setStatus({ kind: 'error', message: `Could not load saved credentials: ${String(err)}` });
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    invoke('get_backend_log_path')
      .then((path) => {
        if (!cancelled && path) setLogPath(String(path));
      })
      .catch(() => {
        // Non-fatal — just omit the log-path hint if it can't be resolved.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const setField = useCallback((key, value) => {
    setCreds((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleSave = useCallback(async () => {
    setStatus({ kind: 'saving', message: 'Saving…' });
    try {
      await invoke('save_db_credentials', { creds });
      setStatus({ kind: 'reconnecting', message: 'Saved — reconnecting…' });
      const ok = await waitForHealth();
      setStatus(
        ok
          ? { kind: 'connected', message: 'Connected' }
          : {
              kind: 'error',
              message: 'Saved, but the backend did not come back. Check your credentials.',
            },
      );
    } catch (err) {
      // The Rust command returns Err(String) on any failure — surface it.
      setStatus({ kind: 'error', message: String(err) });
    }
  }, [creds]);

  const busy = status.kind === 'saving' || status.kind === 'reconnecting';

  return (
    <section className={styles.dbSection} data-testid="db-settings">
      <h2 className={styles.sectionTitle}>Database connection</h2>
      <p className={styles.sectionHint}>
        Credentials are stored locally on this machine and used to connect to the data warehouse.
      </p>

      <div className={styles.dbGrid}>
        <label className={styles.field}>
          <span className={styles.fieldLabel}>Host</span>
          <input
            className={styles.input}
            type="text"
            value={creds.host}
            onChange={(e) => setField('host', e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.fieldLabel}>Port</span>
          <input
            className={styles.input}
            type="text"
            inputMode="numeric"
            value={creds.port}
            onChange={(e) => setField('port', e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.fieldLabel}>Database</span>
          <input
            className={styles.input}
            type="text"
            value={creds.db}
            onChange={(e) => setField('db', e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.fieldLabel}>SSL mode</span>
          <select
            className={styles.input}
            value={creds.sslmode}
            onChange={(e) => setField('sslmode', e.target.value)}
          >
            <option value="require">require</option>
            <option value="prefer">prefer</option>
            <option value="disable">disable</option>
          </select>
        </label>

        <label className={styles.field}>
          <span className={styles.fieldLabel}>DWH user (read-only)</span>
          <input
            className={styles.input}
            type="text"
            value={creds.dwh_user}
            onChange={(e) => setField('dwh_user', e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.fieldLabel}>DWH password</span>
          <input
            className={styles.input}
            type="password"
            value={creds.dwh_password}
            onChange={(e) => setField('dwh_password', e.target.value)}
            autoComplete="new-password"
          />
        </label>

        <label className={styles.field}>
          <span className={styles.fieldLabel}>App-data user (read-write)</span>
          <input
            className={styles.input}
            type="text"
            value={creds.app_db_user}
            onChange={(e) => setField('app_db_user', e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>

        <label className={styles.field}>
          <span className={styles.fieldLabel}>App-data password</span>
          <input
            className={styles.input}
            type="password"
            value={creds.app_db_password}
            onChange={(e) => setField('app_db_password', e.target.value)}
            autoComplete="new-password"
          />
        </label>
      </div>

      <div className={styles.dbActions}>
        <button
          type="button"
          className={`${styles.optionBtn} ${styles.saveBtn}`}
          onClick={handleSave}
          disabled={busy || !loaded}
          data-testid="db-save-btn"
        >
          {busy ? 'Working…' : 'Save & reconnect'}
        </button>
        {status.message ? (
          <span
            className={`${styles.dbStatus} ${
              status.kind === 'error'
                ? styles.dbStatusError
                : status.kind === 'connected'
                  ? styles.dbStatusOk
                  : ''
            }`}
            role="status"
            data-testid="db-status"
          >
            {status.message}
          </span>
        ) : null}
      </div>

      {logPath ? (
        <p className={styles.logHint} data-testid="db-log-path">
          Backend log: <code className={styles.logPath}>{logPath}</code>
        </p>
      ) : null}
    </section>
  );
}

export default DatabaseSettings;
