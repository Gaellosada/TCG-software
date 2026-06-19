// @vitest-environment jsdom
//
// SettingsPage — tests covering the risk-free rate row (TC4.6–TC4.8).
// Theme and chart-type rows are exercised only incidentally; the focus is
// on the new number input and its localStorage interaction.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import SettingsPage from './SettingsPage';

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
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
