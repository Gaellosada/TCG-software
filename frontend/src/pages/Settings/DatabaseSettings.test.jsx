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

import DatabaseSettings from './DatabaseSettings';

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
