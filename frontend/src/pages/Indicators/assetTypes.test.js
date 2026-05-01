// Unit tests for the asset-type vocabulary and inferAssetType helper.
//
// Sign 4 — no magic strings. The tests reference the exported constants
// for the positive cases.

import { describe, it, expect } from 'vitest';
import {
  INDEX,
  EQUITY,
  OPTION,
  ASSET_TYPES,
  inferAssetType,
} from './assetTypes';

describe('asset-type constants', () => {
  it('exports the three canonical lowercase literals', () => {
    expect(INDEX).toBe('index');
    expect(EQUITY).toBe('equity');
    expect(OPTION).toBe('option');
  });

  it('ASSET_TYPES contains exactly the three values, frozen', () => {
    expect(Array.isArray(ASSET_TYPES)).toBe(true);
    expect(ASSET_TYPES).toHaveLength(3);
    expect(new Set(ASSET_TYPES)).toEqual(new Set([INDEX, EQUITY, OPTION]));
    expect(Object.isFrozen(ASSET_TYPES)).toBe(true);
  });
});

describe('inferAssetType — positive cases', () => {
  it('classifies INDEX collection as index', () => {
    expect(
      inferAssetType({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX' }),
    ).toBe(INDEX);
  });

  it('classifies OPT_* collection as option', () => {
    expect(
      inferAssetType({ type: 'spot', collection: 'OPT_SP_500', instrument_id: 'X' }),
    ).toBe(OPTION);
  });

  it('classifies FUT_* continuous stream as equity', () => {
    expect(
      inferAssetType({
        type: 'continuous',
        collection: 'FUT_ES',
        adjustment: 'none',
        cycle: 'M',
        rollOffset: 0,
        strategy: 'front_month',
      }),
    ).toBe(EQUITY);
  });

  it('classifies FUT_* spot as equity (prefix-only logic)', () => {
    expect(
      inferAssetType({ type: 'spot', collection: 'FUT_SP_500', instrument_id: 'Y' }),
    ).toBe(EQUITY);
  });

  it('classifies ETF / FOREX / FUND as equity', () => {
    expect(inferAssetType({ type: 'spot', collection: 'ETF', instrument_id: 'SPY' })).toBe(EQUITY);
    expect(inferAssetType({ type: 'spot', collection: 'FOREX', instrument_id: 'EURUSD' })).toBe(EQUITY);
    expect(inferAssetType({ type: 'spot', collection: 'FUND', instrument_id: 'VTSAX' })).toBe(EQUITY);
  });
});

describe('inferAssetType — null / unknown cases', () => {
  it('returns null for null', () => {
    expect(inferAssetType(null)).toBe(null);
  });

  it('returns null for undefined', () => {
    expect(inferAssetType(undefined)).toBe(null);
  });

  it('returns null for non-object input', () => {
    expect(inferAssetType('INDEX')).toBe(null);
    expect(inferAssetType(42)).toBe(null);
  });

  it('returns null for missing collection', () => {
    expect(inferAssetType({ type: 'spot', instrument_id: 'X' })).toBe(null);
  });

  it('returns null for empty-string collection', () => {
    expect(inferAssetType({ type: 'spot', collection: '', instrument_id: 'X' })).toBe(null);
  });

  it('returns null for unknown collection (no prefix match)', () => {
    expect(inferAssetType({ type: 'spot', collection: 'CRYPTO', instrument_id: 'BTC' })).toBe(null);
    expect(inferAssetType({ type: 'spot', collection: 'INDEX_OLD', instrument_id: 'X' })).toBe(null);
  });
});
