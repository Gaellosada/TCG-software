// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

// Mock the Tauri IPC bridge: the real `invoke` throws outside a Tauri webview.
// We control its resolved/rejected value per command, keyed by command NAME
// (not call order) — the component fires several independent invokes on mount
// (`get_db_credentials`, `get_backend_log_path`), so order-based mocking would
// be brittle.
const invoke = vi.fn();
vi.mock('@tauri-apps/api/core', () => ({ invoke: (...args) => invoke(...args) }));

import DatabaseSettings, { looksLikeSpawnBlock } from './DatabaseSettings';

const STORED = {
  host: 'db.example.com',
  port: '5432',
  db: 'dwh',
  sslmode: 'require',
  dwh_user: 'tcg_read',
  dwh_password: 'ro-secret',
  app_db_user: 'tcg_app_rw',
  app_db_password: 'rw-secret',
};

const LOG_PATH = '/home/u/.local/share/com.trajectoirecap.tcg/logs/backend.log';

// Install a name-dispatching default for `invoke`. Per-test overrides (e.g. a
// failing save) wrap this so only the command under test changes.
function mockInvoke({ creds = STORED, save = undefined, logPath = LOG_PATH } = {}) {
  invoke.mockImplementation((cmd) => {
    switch (cmd) {
      case 'get_db_credentials':
        return Promise.resolve(creds);
      case 'get_backend_log_path':
        return Promise.resolve(logPath);
      case 'save_db_credentials':
        return save instanceof Error ? Promise.reject(save) : Promise.resolve(save);
      default:
        return Promise.resolve(undefined);
    }
  });
}

beforeEach(() => {
  invoke.mockReset();
  mockInvoke();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('<DatabaseSettings>', () => {
  it('prefills fields from get_db_credentials on mount', async () => {
    render(<DatabaseSettings />);

    await waitFor(() => {
      expect(screen.getByDisplayValue('db.example.com')).toBeDefined();
    });
    expect(invoke).toHaveBeenCalledWith('get_db_credentials');
    expect(screen.getByDisplayValue('tcg_read')).toBeDefined();
    expect(screen.getByDisplayValue('tcg_app_rw')).toBeDefined();
  });

  it('shows the backend log path fetched from get_backend_log_path', async () => {
    render(<DatabaseSettings />);
    await waitFor(() => {
      expect(screen.getByTestId('db-log-path')).toBeDefined();
    });
    expect(invoke).toHaveBeenCalledWith('get_backend_log_path');
    expect(screen.getByTestId('db-log-path').textContent).toContain(LOG_PATH);
  });

  it('Save calls save_db_credentials with the edited creds and polls health to "Connected"', async () => {
    mockInvoke({ save: undefined }); // save resolves Ok

    // Health comes back 200 on the first poll.
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue({ ok: true });

    render(<DatabaseSettings />);
    await waitFor(() => expect(screen.getByDisplayValue('db.example.com')).toBeDefined());

    // Edit the host.
    const hostInput = screen.getByDisplayValue('db.example.com');
    fireEvent.change(hostInput, { target: { value: 'new-host' } });

    fireEvent.click(screen.getByTestId('db-save-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('db-status').textContent).toBe('Connected');
    });

    // save_db_credentials called with { creds: {...host: 'new-host'} }.
    const saveCall = invoke.mock.calls.find((c) => c[0] === 'save_db_credentials');
    expect(saveCall).toBeTruthy();
    expect(saveCall[1].creds.host).toBe('new-host');
    expect(saveCall[1].creds.app_db_user).toBe('tcg_app_rw');
    expect(fetchSpy).toHaveBeenCalledWith('http://127.0.0.1:8000/health', expect.any(Object));
  });

  it('surfaces the Err(string) returned by save_db_credentials', async () => {
    // save_db_credentials rejects; mount calls still resolve normally.
    mockInvoke({ save: new Error('could not write /cfg/.env: permission denied') });

    render(<DatabaseSettings />);
    await waitFor(() => expect(screen.getByDisplayValue('db.example.com')).toBeDefined());

    fireEvent.click(screen.getByTestId('db-save-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('db-status').textContent).toContain('permission denied');
    });
  });

  it('renders the antivirus/spawn hint when save fails with an os-error message', async () => {
    mockInvoke({
      save: new Error(
        'saved credentials, but backend restart failed: spawn failed (os error 5): ' +
          'Access is denied. | resolved sidecar path=C:\\TCG\\tcg-backend.exe exists=yes size=8B',
      ),
    });

    render(<DatabaseSettings />);
    await waitFor(() => expect(screen.getByDisplayValue('db.example.com')).toBeDefined());

    fireEvent.click(screen.getByTestId('db-save-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('db-spawn-hint')).toBeDefined();
    });
    // Raw detail is still surfaced alongside the plain-language hint.
    expect(screen.getByTestId('db-status').textContent).toContain('os error 5');
    expect(screen.getByTestId('db-spawn-hint').textContent).toMatch(/antivirus/i);
  });

  it('does NOT render the spawn hint on a realistic .env-write failure (carries "os error")', async () => {
    // Rust's io::Error Display always appends "(os error N)", so a plain
    // .env-write failure ALSO contains that fragment — the hint must key off the
    // "spawn failed" marker, not "os error", or this would false-positive.
    mockInvoke({
      save: new Error(
        'could not write /home/u/.config/com.trajectoirecap.tcg/.env: Permission denied (os error 13)',
      ),
    });

    render(<DatabaseSettings />);
    await waitFor(() => expect(screen.getByDisplayValue('db.example.com')).toBeDefined());

    fireEvent.click(screen.getByTestId('db-save-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('db-status').textContent).toContain('os error 13');
    });
    expect(screen.queryByTestId('db-spawn-hint')).toBeNull();
  });

  it('does NOT render the spawn hint on a successful save', async () => {
    mockInvoke({ save: undefined });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true });

    render(<DatabaseSettings />);
    await waitFor(() => expect(screen.getByDisplayValue('db.example.com')).toBeDefined());

    fireEvent.click(screen.getByTestId('db-save-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('db-status').textContent).toBe('Connected');
    });
    expect(screen.queryByTestId('db-spawn-hint')).toBeNull();
  });

  it('looksLikeSpawnBlock matches the "spawn failed" marker only (not any "os error")', () => {
    // True spawn blocks carry the literal `spawn failed` prefix.
    expect(looksLikeSpawnBlock('spawn failed (os error 32): ...')).toBe(true);
    expect(looksLikeSpawnBlock('backend restart failed: spawn failed (os error 5)')).toBe(true);
    // A bare "(os error N)" is NOT sufficient — io::Error Display always appends
    // it, so .env-write failures carry it too and must not match.
    expect(looksLikeSpawnBlock('Error: (os error 2)')).toBe(false);
    expect(
      looksLikeSpawnBlock(
        'could not write /home/u/.config/com.trajectoirecap.tcg/.env: Permission denied (os error 13)',
      ),
    ).toBe(false);
    expect(
      looksLikeSpawnBlock('could not create config dir /home/u/.config/x: Permission denied (os error 13)'),
    ).toBe(false);
    // Normal errors and the degraded-health message must not match.
    expect(looksLikeSpawnBlock('could not write /cfg/.env: permission denied')).toBe(false);
    expect(looksLikeSpawnBlock('Saved, but the backend did not come back.')).toBe(false);
    expect(looksLikeSpawnBlock('')).toBe(false);
    expect(looksLikeSpawnBlock(undefined)).toBe(false);
  });

  it('shows a degraded message when the backend never comes back after save', async () => {
    mockInvoke({ save: undefined }); // save resolves Ok

    // Health always fails; waitForHealth eventually returns false. Use fake
    // timers so the poll loop resolves without a real wall-clock wait.
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('refused'));
    vi.useFakeTimers();

    render(<DatabaseSettings />);
    // Flush the mount promise.
    await vi.waitFor(() => expect(screen.getByDisplayValue('db.example.com')).toBeDefined());

    fireEvent.click(screen.getByTestId('db-save-btn'));
    // Advance through all poll intervals (runAllTimersAsync drains them all).
    await vi.runAllTimersAsync();
    vi.useRealTimers();

    await waitFor(() => {
      expect(screen.getByTestId('db-status').textContent).toContain('did not come back');
    });
  });
});
