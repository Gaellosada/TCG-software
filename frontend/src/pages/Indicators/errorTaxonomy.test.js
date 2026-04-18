import { describe, it, expect } from 'vitest';
import {
  INDICATOR_ERROR_TYPES,
  HEADINGS,
  ABORTED,
  fetchKindToErrorType,
  coerceErrorType,
} from './errorTaxonomy';

describe('INDICATOR_ERROR_TYPES / HEADINGS', () => {
  it('every canonical error type has a matching heading', () => {
    for (const type of INDICATOR_ERROR_TYPES) {
      expect(HEADINGS[type]).toBeTruthy();
    }
  });

  it('HEADINGS has no stray keys outside the canonical list', () => {
    const canonical = new Set(INDICATOR_ERROR_TYPES);
    for (const key of Object.keys(HEADINGS)) {
      expect(canonical.has(key)).toBe(true);
    }
  });
});

describe('fetchKindToErrorType', () => {
  it('maps aborted to the ABORTED sentinel', () => {
    expect(fetchKindToErrorType('aborted')).toBe(ABORTED);
  });

  it('maps offline to offline', () => {
    expect(fetchKindToErrorType('offline')).toBe('offline');
  });

  it('maps network and server to network', () => {
    expect(fetchKindToErrorType('network')).toBe('network');
    expect(fetchKindToErrorType('server')).toBe('network');
  });

  it('maps client to validation', () => {
    expect(fetchKindToErrorType('client')).toBe('validation');
  });

  it('falls back to runtime for unknown kinds', () => {
    expect(fetchKindToErrorType('unknown')).toBe('runtime');
    expect(fetchKindToErrorType(undefined)).toBe('runtime');
  });
});

describe('coerceErrorType', () => {
  it('passes through canonical values', () => {
    for (const t of INDICATOR_ERROR_TYPES) {
      expect(coerceErrorType(t)).toBe(t);
    }
  });

  it('defaults unknown values to validation', () => {
    expect(coerceErrorType('bogus')).toBe('validation');
    expect(coerceErrorType(undefined)).toBe('validation');
    expect(coerceErrorType(null)).toBe('validation');
  });
});
