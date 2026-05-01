import { describe, it, expect } from 'vitest';
import {
  INDICATOR_ERROR_TYPES,
  HEADINGS,
  ABORTED,
  ERROR_CODE_TO_TYPE,
  fetchKindToErrorType,
  coerceErrorType,
  errorCodeToType,
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

  it('includes incompatible_asset as a canonical error type', () => {
    expect(INDICATOR_ERROR_TYPES).toContain('incompatible_asset');
    expect(HEADINGS.incompatible_asset).toBeTruthy();
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

describe('ERROR_CODE_TO_TYPE / errorCodeToType', () => {
  it('maps INDICATOR_INCOMPATIBLE_ASSET to incompatible_asset', () => {
    expect(ERROR_CODE_TO_TYPE.INDICATOR_INCOMPATIBLE_ASSET).toBe('incompatible_asset');
    expect(errorCodeToType('INDICATOR_INCOMPATIBLE_ASSET')).toBe('incompatible_asset');
  });

  it('returns null for unknown / empty / non-string codes', () => {
    expect(errorCodeToType('UNKNOWN_CODE')).toBeNull();
    expect(errorCodeToType('')).toBeNull();
    expect(errorCodeToType(undefined)).toBeNull();
    expect(errorCodeToType(null)).toBeNull();
    expect(errorCodeToType(42)).toBeNull();
  });

  it('every value in ERROR_CODE_TO_TYPE is a canonical error type', () => {
    const canonical = new Set(INDICATOR_ERROR_TYPES);
    for (const v of Object.values(ERROR_CODE_TO_TYPE)) {
      expect(canonical.has(v)).toBe(true);
    }
  });
});
