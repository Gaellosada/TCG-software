// @vitest-environment jsdom
//
// SettingsPage — tests covering the risk-free rate row (TC4.6–TC4.8).
// Theme and chart-type rows are exercised only incidentally; the focus is
// on the new number input and its localStorage interaction.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';

// Mock the Tauri app namespace so the desktop-only version row is testable
// without a real webview. `getVersion` resolves to a fixed SENTINEL string
// (deliberately NOT the real app version — this proves the row renders whatever
// getVersion returns instead of a hardcoded value); `invoke` (used by the
// DatabaseSettings child once it mounts under Tauri) is stubbed so it never
// hits the real bridge. These mocks are inert in web-mode tests, which never
// set window.__TAURI_INTERNALS__, so isTauri() stays false there.
const getVersion = vi.fn(() => Promise.resolve('9.9.9-test'));
vi.mock('@tauri-apps/api/app', () => ({ getVersion: (...a) => getVersion(...a) }));
vi.mock('@tauri-apps/api/core', () => ({ invoke: () => Promise.resolve(undefined) }));

// The Clear-cache button hits the backend; mock the api so no HTTP is attempted.
const clearPortfolioCache = vi.fn(() => Promise.resolve({ cleared: true }));
vi.mock('../../api/portfolio', () => ({
  clearPortfolioCache: (...a) => clearPortfolioCache(...a),
}));

import SettingsPage from './SettingsPage';

beforeEach(() => {
  localStorage.clear();
  getVersion.mockClear();
  clearPortfolioCache.mockClear();
});

afterEach(() => {
  cleanup();
  delete window.__TAURI_INTERNALS__;
});

describe('<SettingsPage> — risk-free rate row', () => {
  // TC4.6
  it('renders the risk-free rate input with default value "4.00" when localStorage is empty', () => {
    render(<SettingsPage />);
    const input = screen.getByLabelText(/default risk-free rate/i);
    expect(input).toBeTruthy();
    expect(input.value).toBe('4.00');
  });

  it('renders with a stored value when localStorage has one', () => {
    localStorage.setItem('tcg-risk-free-rate', '3.50');
    render(<SettingsPage />);
    const input = screen.getByLabelText(/default risk-free rate/i);
    expect(input.value).toBe('3.50');
  });

  // TC4.7
  it('persists a valid positive value to localStorage on change', () => {
    render(<SettingsPage />);
    const input = screen.getByLabelText(/default risk-free rate/i);
    fireEvent.change(input, { target: { value: '5.00' } });
    expect(localStorage.getItem('tcg-risk-free-rate')).toBe('5.00');
  });

  it('persists zero to localStorage', () => {
    render(<SettingsPage />);
    const input = screen.getByLabelText(/default risk-free rate/i);
    fireEvent.change(input, { target: { value: '0' } });
    expect(localStorage.getItem('tcg-risk-free-rate')).toBe('0');
  });

  // TC4.8
  it('does NOT write to localStorage when the input is a negative number', () => {
    render(<SettingsPage />);
    const input = screen.getByLabelText(/default risk-free rate/i);
    fireEvent.change(input, { target: { value: '-1' } });
    expect(localStorage.getItem('tcg-risk-free-rate')).toBeNull();
  });

  it('does NOT write to localStorage when the input is non-numeric', () => {
    render(<SettingsPage />);
    const input = screen.getByLabelText(/default risk-free rate/i);
    fireEvent.change(input, { target: { value: 'abc' } });
    expect(localStorage.getItem('tcg-risk-free-rate')).toBeNull();
  });

  it('renders the "%" unit label next to the input', () => {
    render(<SettingsPage />);
    expect(screen.getByText('%')).toBeTruthy();
  });

  it('renders the "Default risk-free rate" row label', () => {
    render(<SettingsPage />);
    expect(screen.getByText(/default risk-free rate/i)).toBeTruthy();
  });

  it('renders the hint text about ratios', () => {
    render(<SettingsPage />);
    expect(screen.getByText(/sharpe, sortino/i)).toBeTruthy();
  });
});

describe('<SettingsPage> — portfolio-result cache toggle (backend flag)', () => {
  it('defaults to ON when localStorage is empty', () => {
    render(<SettingsPage />);
    expect(screen.getByTestId('portfolio-cache-on').getAttribute('aria-checked')).toBe('true');
    expect(screen.getByTestId('portfolio-cache-off').getAttribute('aria-checked')).toBe('false');
  });

  it('reflects a stored "false" as OFF', () => {
    localStorage.setItem('tcg-portfolio-cache-enabled', 'false');
    render(<SettingsPage />);
    expect(screen.getByTestId('portfolio-cache-off').getAttribute('aria-checked')).toBe('true');
  });

  it('writes String(true)/String(false) to localStorage on toggle', () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByTestId('portfolio-cache-off'));
    expect(localStorage.getItem('tcg-portfolio-cache-enabled')).toBe('false');
    fireEvent.click(screen.getByTestId('portfolio-cache-on'));
    expect(localStorage.getItem('tcg-portfolio-cache-enabled')).toBe('true');
  });

  it('Clear button calls the backend clear endpoint and acknowledges', async () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByTestId('clear-cache-btn'));
    expect(clearPortfolioCache).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.getByTestId('cache-cleared')).toBeTruthy();
    });
  });
});

describe('<SettingsPage> — desktop-only DB credentials section', () => {
  it('does NOT render the Database connection section in web mode (no Tauri global)', () => {
    // isTauri() is false under jsdom, so the desktop-only credentials editor
    // must not mount — guaranteeing the web build stays unchanged and the real
    // Tauri `invoke` is never called from these tests.
    render(<SettingsPage />);
    expect(screen.queryByTestId('db-settings')).toBeNull();
    expect(screen.queryByText('Database connection')).toBeNull();
  });
});

describe('<SettingsPage> — desktop-only app version footer', () => {
  it('does NOT render the version footer in web mode and never calls getVersion', () => {
    render(<SettingsPage />);
    expect(screen.queryByTestId('app-version')).toBeNull();
    expect(getVersion).not.toHaveBeenCalled();
  });

  it('under Tauri, shows "Version <x>" from getVersion()', async () => {
    window.__TAURI_INTERNALS__ = {};
    render(<SettingsPage />);
    await waitFor(() => {
      expect(screen.getByTestId('app-version')).toBeDefined();
    });
    expect(getVersion).toHaveBeenCalled();
    expect(screen.getByTestId('app-version').textContent).toBe('Version 9.9.9-test');
  });
});
