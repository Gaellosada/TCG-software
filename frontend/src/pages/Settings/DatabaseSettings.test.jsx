// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

// Mock the Tauri IPC bridge: the real `invoke` throws outside a Tauri webview.
// We control its resolved/rejected value per test.
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

beforeEach(() => {
  invoke.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('<DatabaseSettings>', () => {
  it('prefills fields from get_db_credentials on mount', async () => {
    invoke.mockResolvedValueOnce(STORED); // get_db_credentials
    render(<DatabaseSettings />);

    await waitFor(() => {
      expect(screen.getByDisplayValue('db.example.com')).toBeDefined();
    });
    expect(invoke).toHaveBeenCalledWith('get_db_credentials');
    expect(screen.getByDisplayValue('tcg_read')).toBeDefined();
    expect(screen.getByDisplayValue('tcg_app_rw')).toBeDefined();
  });

  it('Save calls save_db_credentials with the edited creds and polls health to "Connected"', async () => {
    invoke
      .mockResolvedValueOnce(STORED) // get_db_credentials (mount)
      .mockResolvedValueOnce(undefined); // save_db_credentials (Ok)

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
    invoke
      .mockResolvedValueOnce(STORED) // mount
      .mockRejectedValueOnce('could not write /cfg/.env: permission denied'); // save fails

    render(<DatabaseSettings />);
    await waitFor(() => expect(screen.getByDisplayValue('db.example.com')).toBeDefined());

    fireEvent.click(screen.getByTestId('db-save-btn'));

    await waitFor(() => {
      expect(screen.getByTestId('db-status').textContent).toContain('permission denied');
    });
  });

  it('shows a degraded message when the backend never comes back after save', async () => {
    invoke
      .mockResolvedValueOnce(STORED) // mount
      .mockResolvedValueOnce(undefined); // save Ok

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
