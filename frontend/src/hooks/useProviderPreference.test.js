/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import useProviderPreference, { STORAGE_KEY, EVENT_NAME } from './useProviderPreference';

const dispatchSpy = vi.spyOn(window, 'dispatchEvent');

function clearProviderStorage() {
  localStorage.removeItem(STORAGE_KEY);
}

describe('useProviderPreference', () => {
  beforeEach(() => {
    clearProviderStorage();
    dispatchSpy.mockClear();
  });

  it('getDefault returns null when nothing stored', () => {
    const { getDefault } = useProviderPreference();
    expect(getDefault('INDEX')).toBeNull();
  });

  it('getDefault returns exact match', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ INDEX: 'BLOOMBERG', ETF: 'YAHOO' }));
    const { getDefault } = useProviderPreference();
    expect(getDefault('INDEX')).toBe('BLOOMBERG');
    expect(getDefault('ETF')).toBe('YAHOO');
  });

  it('getDefault returns prefix match for keys ending with _', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ 'FUT_': 'IVOLATILITY' }));
    const { getDefault } = useProviderPreference();
    expect(getDefault('FUT_VX')).toBe('IVOLATILITY');
    expect(getDefault('FUT_ES')).toBe('IVOLATILITY');
  });

  it('getDefault prefers exact match over prefix match', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ 'FUT_': 'IVOLATILITY', 'FUT_VX': 'DERIBIT' }));
    const { getDefault } = useProviderPreference();
    expect(getDefault('FUT_VX')).toBe('DERIBIT');
  });

  it('getDefault returns null for non-matching collection', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ INDEX: 'YAHOO' }));
    const { getDefault } = useProviderPreference();
    expect(getDefault('FOREX')).toBeNull();
  });

  it('setDefault writes to localStorage and dispatches event', () => {
    const { setDefault, getDefault } = useProviderPreference();
    setDefault('INDEX', 'BLOOMBERG');

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
    expect(stored).toEqual({ INDEX: 'BLOOMBERG' });

    expect(dispatchSpy).toHaveBeenCalledTimes(1);
    const event = dispatchSpy.mock.calls[0][0];
    expect(event.type).toBe(EVENT_NAME);
    expect(event.detail).toEqual({ key: 'INDEX', provider: 'BLOOMBERG' });

    // Subsequent getDefault reads the updated value
    expect(getDefault('INDEX')).toBe('BLOOMBERG');
  });

  it('setDefault merges with existing values', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ INDEX: 'YAHOO' }));
    const { setDefault } = useProviderPreference();
    setDefault('ETF', 'BLOOMBERG');

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
    expect(stored).toEqual({ INDEX: 'YAHOO', ETF: 'BLOOMBERG' });
  });

  it('handles corrupt localStorage gracefully', () => {
    localStorage.setItem(STORAGE_KEY, 'not-json');
    const { getDefault } = useProviderPreference();
    expect(getDefault('INDEX')).toBeNull();
  });
});
