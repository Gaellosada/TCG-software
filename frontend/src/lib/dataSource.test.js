import { describe, it, expect } from 'vitest';
import {
  DATA_SOURCE_V1,
  DATA_SOURCE_V2,
  DEFAULT_DATA_SOURCE,
  coerceDataSource,
  dataSourceFieldsForRequest,
  V2_LIMITATIONS,
} from './dataSource';

describe('dataSourceFieldsForRequest', () => {
  it('emits NOTHING for v1 — a default body stays byte-identical', () => {
    expect(dataSourceFieldsForRequest(DATA_SOURCE_V1)).toEqual({});
    expect(Object.keys(dataSourceFieldsForRequest('v1'))).toHaveLength(0);
  });

  it('emits nothing for undefined / null / garbage', () => {
    expect(dataSourceFieldsForRequest(undefined)).toEqual({});
    expect(dataSourceFieldsForRequest(null)).toEqual({});
    expect(dataSourceFieldsForRequest('V2')).toEqual({});
    expect(dataSourceFieldsForRequest(2)).toEqual({});
  });

  it('emits data_source only for v2', () => {
    expect(dataSourceFieldsForRequest(DATA_SOURCE_V2)).toEqual({ data_source: 'v2' });
  });
});

describe('coerceDataSource', () => {
  it('defaults to v1 for anything that is not exactly "v2"', () => {
    expect(DEFAULT_DATA_SOURCE).toBe('v1');
    for (const bad of [undefined, null, '', 'v3', 'V2', 0, {}]) {
      expect(coerceDataSource(bad)).toBe('v1');
    }
  });

  it('passes v2 through', () => {
    expect(coerceDataSource('v2')).toBe('v2');
  });
});

describe('V2_LIMITATIONS', () => {
  it('names every measured limit the user could be misled by', () => {
    const all = V2_LIMITATIONS.join(' ');
    expect(all).toContain('IND_SP_500');
    expect(all).toContain('OPT_SP_500');
    expect(all).toMatch(/Monthly \(M\)/);
    expect(all).toContain('mid');
    expect(all).toContain('2016-02-22');
    expect(all).toContain('2010-06-07');
    expect(all).toContain('2026-06-12');
    expect(all).toContain('2026-07-21');
    expect(all).toMatch(/NaN/);
  });
});
