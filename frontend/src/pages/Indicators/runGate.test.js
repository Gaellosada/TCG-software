import { describe, it, expect } from 'vitest';
import {
  areAllSlotsFilled,
  computeRunDisabledReason,
  deriveAssetTypeFromSeriesMap,
  computeAssetCompatibility,
} from './runGate';

const SPOT_INDEX = { type: 'spot', collection: 'INDEX', instrument_id: 'IND_SP_500' };
const SPOT_OPTION = { type: 'spot', collection: 'OPT_SPX', instrument_id: 'SPXW 250101' };
const SPOT_EQUITY = { type: 'spot', collection: 'ETF', instrument_id: 'SPY' };
const CONT_FUT = { type: 'continuous', collection: 'FUT_ES', adjustment: 'none' };

const SMA_CODE = 'def compute(series, window: int = 20):\n    s = series["close"]\n    return s';

function makeInd({ code = SMA_CODE, seriesMap = {}, compatibleAssetTypes } = {}) {
  const ind = { id: 'x', name: 'X', code, params: {}, seriesMap };
  if (compatibleAssetTypes !== undefined) ind.compatibleAssetTypes = compatibleAssetTypes;
  return ind;
}

describe('areAllSlotsFilled', () => {
  it('false with no indicator', () => {
    expect(areAllSlotsFilled(null, ['close'])).toBe(false);
  });

  it('false when no labels declared (defensive)', () => {
    expect(areAllSlotsFilled(makeInd(), [])).toBe(false);
  });

  it('false when a slot is null', () => {
    expect(areAllSlotsFilled(makeInd({ seriesMap: { close: null } }), ['close'])).toBe(false);
  });

  it('true with a fully-populated spot slot', () => {
    expect(areAllSlotsFilled(
      makeInd({ seriesMap: { close: SPOT_INDEX } }),
      ['close'],
    )).toBe(true);
  });

  it('true with a continuous slot lacking instrument_id', () => {
    expect(areAllSlotsFilled(
      makeInd({ seriesMap: { close: CONT_FUT } }),
      ['close'],
    )).toBe(true);
  });

  it('false on a spot slot missing instrument_id', () => {
    expect(areAllSlotsFilled(
      makeInd({ seriesMap: { close: { type: 'spot', collection: 'INDEX' } } }),
      ['close'],
    )).toBe(false);
  });
});

describe('deriveAssetTypeFromSeriesMap', () => {
  it('returns null when seriesMap is empty', () => {
    expect(deriveAssetTypeFromSeriesMap({})).toEqual({ ok: true, asset_type: null });
  });

  it('returns null when seriesMap is missing entirely', () => {
    expect(deriveAssetTypeFromSeriesMap(null)).toEqual({ ok: true, asset_type: null });
    expect(deriveAssetTypeFromSeriesMap(undefined)).toEqual({ ok: true, asset_type: null });
  });

  it('returns the unique inferred type when slots agree', () => {
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_INDEX })).toEqual({ ok: true, asset_type: 'index' });
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_OPTION })).toEqual({ ok: true, asset_type: 'option' });
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_EQUITY, b: CONT_FUT })).toEqual({ ok: true, asset_type: 'equity' });
  });

  it('returns slot_conflict when slots disagree', () => {
    const out = deriveAssetTypeFromSeriesMap({ a: SPOT_INDEX, b: SPOT_OPTION });
    expect(out.ok).toBe(false);
    expect(out.reason).toBe('slot_conflict');
    expect(out.types.sort()).toEqual(['index', 'option']);
  });

  it('skips null slots without conflict', () => {
    expect(deriveAssetTypeFromSeriesMap({ a: SPOT_INDEX, b: null })).toEqual({ ok: true, asset_type: 'index' });
  });

  it('skips slots whose collection is unknown (returns null type)', () => {
    expect(deriveAssetTypeFromSeriesMap({
      a: SPOT_INDEX,
      b: { type: 'spot', collection: 'CRYPTO', instrument_id: 'BTC' },
    })).toEqual({ ok: true, asset_type: 'index' });
  });
});

describe('computeAssetCompatibility', () => {
  it('returns ok for a null indicator', () => {
    expect(computeAssetCompatibility(null)).toEqual({ ok: true });
  });

  it('returns ok when compatibleAssetTypes is missing (back-compat)', () => {
    expect(computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_OPTION } }),
    )).toEqual({ ok: true });
  });

  it('returns ok when compatibleAssetTypes is an empty array', () => {
    // Empty array is treated as "no compat declared" — never silently
    // blocking forever from a misconfigured registry.
    expect(computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_OPTION }, compatibleAssetTypes: [] }),
    )).toEqual({ ok: true });
  });

  it('returns ok when slots agree and the type is in the compat list', () => {
    expect(computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_INDEX }, compatibleAssetTypes: ['index', 'equity'] }),
    )).toEqual({ ok: true });
  });

  it('rejects with incompatible_asset when slot type is not in compat list', () => {
    const out = computeAssetCompatibility(
      makeInd({ seriesMap: { close: SPOT_OPTION }, compatibleAssetTypes: ['index', 'equity'] }),
    );
    expect(out.ok).toBe(false);
    expect(out.reason).toBe('incompatible_asset');
    expect(out.asset_type).toBe('option');
    expect(out.accepted_asset_types).toEqual(['index', 'equity']);
  });

  it('propagates slot_conflict from underlying derivation', () => {
    const out = computeAssetCompatibility(
      makeInd({
        seriesMap: { a: SPOT_INDEX, b: SPOT_OPTION },
        compatibleAssetTypes: ['index'],
      }),
    );
    expect(out.ok).toBe(false);
    expect(out.reason).toBe('slot_conflict');
  });

  it('returns ok when seriesMap has no inferable type (defer to backend)', () => {
    expect(computeAssetCompatibility(
      makeInd({
        seriesMap: { close: { type: 'spot', collection: 'CRYPTO', instrument_id: 'BTC' } },
        compatibleAssetTypes: ['index'],
      }),
    )).toEqual({ ok: true });
  });
});

describe('computeRunDisabledReason', () => {
  it('asks to select an indicator first', () => {
    expect(computeRunDisabledReason(null, ['close'])).toBe('Select an indicator first');
  });

  it('asks to fill empty slot', () => {
    expect(computeRunDisabledReason(
      makeInd({ seriesMap: { close: null } }),
      ['close'],
    )).toBe('Fill series slot: close');
  });

  it('reports asset-type incompatibility once slots are filled', () => {
    const reason = computeRunDisabledReason(
      makeInd({
        seriesMap: { close: SPOT_OPTION },
        compatibleAssetTypes: ['index', 'equity'],
      }),
      ['close'],
    );
    expect(reason).toContain('Requires');
    expect(reason).toContain('index');
    expect(reason).toContain('equity');
    expect(reason).toContain('option');
  });

  it('reports slot_conflict when slots disagree', () => {
    const reason = computeRunDisabledReason(
      makeInd({
        seriesMap: { a: SPOT_INDEX, b: SPOT_OPTION },
        compatibleAssetTypes: ['index'],
        code: 'def compute(series):\n    return series["a"] + series["b"]',
      }),
      ['a', 'b'],
    );
    expect(reason).toContain('disagree');
    expect(reason).toContain('index');
    expect(reason).toContain('option');
  });
});
